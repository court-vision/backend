"""
A team's automation levels, read and written (the Dime autopilot plan's "Automation
levels" and "AI features and the autopilot switch", step 5; the rules are in
services/ai/autonomy.py).

The table's AI part is usr.autopilot (`ai_rows`, the `autopilot` switch, `restore`);
check-ins are on while the row has no `paused_at`. Lineup's levels below AI are the
team's notification preferences, which data-platform's auto-lineup already reads:

    off      the tip-off email off (and auto-lineup with it)
    notify   the email on, auto-lineup off
    act, ai  both on (at AI, auto-lineup's fill is the plan Dime reviews)

so a lineup change writes the team's override and `ai_rows` in one transaction. The
account's own email gates every team (the pipeline skips a user whose account-level
email is off), so lineup's notify, act and AI are refused while it is off.

The switch, coming on, puts every row that offers AI at AI, turns check-ins and both AI
features on, and turns the team's lineup email and auto-lineup on (the fill is Dime's
tool), keeping what it changed in `restore`; going off, it puts the lineup preferences
(unless the user changed them since) and the check-ins back and the stored rows count
again (the features stay on). Changing a row while it is on turns it off and keeps every
other row at AI. Dime pausing itself leaves it on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from core.errors import AppError, ConflictError
from db.base import db, db_operation
from db.models.autopilot import AiSettings, Autopilot
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from services.ai import autonomy


class LevelUnavailable(ConflictError):
    error_code = "AI_LEVEL_UNAVAILABLE"
    default_message = "That automation can't be set to that level"


@dataclass(frozen=True)
class Change:
    """A settings change: what is left as None (or out of `levels`) stays as it is."""

    checkins: Optional[bool] = None
    autopilot: Optional[bool] = None
    levels: Mapping[str, str] = field(default_factory=dict)


# ------------------------------- lineup's levels below AI ------------------------------- #


def lineup_email(prefs: Any) -> bool:
    """The account's lineup email is on: without it the pipeline runs no team's lineup."""
    return prefs is not None and bool(prefs.lineup_alerts_enabled)


def lineup_below_ai(prefs: Any, team_pref: Any) -> str:
    """off, notify or act, from the preferences as data-platform reads them."""
    from services.ai.automations import fill_setting
    setting = fill_setting(prefs, team_pref)
    if not setting.runs:
        return autonomy.OFF
    return autonomy.ACT if setting.applies else autonomy.NOTIFY


def lineup_prefs_for(level: str) -> dict[str, bool]:
    """What a lineup level writes to the team's override."""
    if level == autonomy.OFF:
        return {"lineup_alerts_enabled": False, "auto_lineup_enabled": False}
    if level == autonomy.NOTIFY:
        return {"lineup_alerts_enabled": True, "auto_lineup_enabled": False}
    return {"lineup_alerts_enabled": True, "auto_lineup_enabled": True}


@db_operation("ai.levels_prefs")
def prefs_of(user_id: int, team_ids: list[int]) -> tuple[Any, dict[int, Any]]:
    """The account's preferences and each team's override (absent: none)."""
    prefs = NotificationPreference.get_or_none(NotificationPreference.user == user_id)
    rows = (NotificationTeamPreference.select()
            .where((NotificationTeamPreference.user == user_id) & (NotificationTeamPreference.team_id.in_(team_ids)))
            if team_ids else [])
    return prefs, {r.team_id: r for r in rows}


def _team_pref(user_id: int, team_id: int) -> Any:
    return (NotificationTeamPreference.select()
            .where((NotificationTeamPreference.user == user_id) & (NotificationTeamPreference.team_id == team_id))
            .first())


def _write_lineup(user_id: int, team_id: int, values: Mapping[str, Optional[bool]]) -> None:
    """The team's lineup override, in one upsert on (user, team_id): two first writes at once (a double
    click) both land, as apply's own insert does."""
    now = datetime.utcnow()                 # the model's own clock (naive UTC)
    update: dict[Any, Any] = {getattr(NotificationTeamPreference, k): v for k, v in values.items()}
    update[NotificationTeamPreference.updated_at] = now
    (NotificationTeamPreference.insert(user=user_id, team_id=team_id, created_at=now, updated_at=now, **values)
     .on_conflict(conflict_target=[NotificationTeamPreference.user, NotificationTeamPreference.team_id],
                  update=update)
     .execute())


def _snapshot_lineup(user_id: int, team_id: int) -> dict[str, Any]:
    row = _team_pref(user_id, team_id)
    if row is None:
        return {"lineup_alerts_enabled": None, "auto_lineup_enabled": None}
    return {"lineup_alerts_enabled": row.lineup_alerts_enabled, "auto_lineup_enabled": row.auto_lineup_enabled}


# ------------------------------- the change ------------------------------- #


def check(change: Change, *, email: bool) -> Change:
    """The change, with `levels.checkins` folded into `checkins`; refuses an unknown row or level
    (422) and a level a row doesn't offer (409, with why)."""
    if change.autopilot is not None and change.levels:
        raise AppError("AI_AUTOPILOT_INVALID", "Set the autopilot switch or levels, not both", status_code=422)
    levels: dict[str, str] = {}
    checkins = change.checkins
    for row, level in change.levels.items():
        if row not in autonomy.ROWS:
            raise AppError("AI_AUTOPILOT_INVALID", f"Unknown automation {row!r}", status_code=422)
        if level not in autonomy.LEVELS:
            raise AppError("AI_AUTOPILOT_INVALID", "A level is off, notify, act or ai", status_code=422)
        reason = autonomy.offered(row, level)
        if reason is None and row == autonomy.LINEUP and level != autonomy.OFF and not email:
            reason = "Your account's lineup email is off, so no team's lineup runs: turn it on in Notifications"
        if reason is not None:
            raise LevelUnavailable(message=f"{autonomy.LABELS[row]} can't be {level}: {reason}")
        if row == autonomy.CHECKINS:
            checkins = level == autonomy.AI
        else:
            levels[row] = level
    if change.checkins is not None and checkins != change.checkins:
        raise AppError("AI_AUTOPILOT_INVALID", "checkins and levels.checkins disagree", status_code=422)
    if checkins is None and change.autopilot is None and not levels:
        raise AppError("AI_AUTOPILOT_INVALID", "Nothing to change: send checkins, autopilot or levels",
                       status_code=422)
    return Change(checkins=checkins, autopilot=change.autopilot, levels=levels)


def _features_on(user_id: int, now: datetime) -> None:
    (AiSettings.insert(user_id=user_id, memory=True, draft_rooms=True, created_at=now, updated_at=now)
     .on_conflict(conflict_target=[AiSettings.user_id],
                  update={AiSettings.memory: True, AiSettings.draft_rooms: True, AiSettings.updated_at: now})
     .execute())


@db_operation("ai.levels_apply")
def apply(team_id: int, user_id: int, change: Change, now: datetime) -> Autopilot:
    """Write a checked change in one transaction; returns the team's row as it now stands."""
    with db.atomic():
        row = Autopilot.get_or_none(Autopilot.team_id == team_id)
        if row is None:
            # Two first switches at once (a double click, a retry): the second insert is a no-op, and
            # the row the first wrote goes through the transitions below like any other. Set up
            # before it is switched on, it starts paused, as the switch left off
            (Autopilot.insert(team_id=team_id, user_id=user_id, created_at=now, updated_at=now, active_since=now,
                              paused_at=now, paused_by="user")
             .on_conflict_ignore()
             .execute())
            row = Autopilot.get(Autopilot.team_id == team_id)
        rows = set(autonomy.stored(row))
        switch = bool(row.autopilot)
        restore = row.restore
        checkins = change.checkins

        if change.autopilot is True and not switch:
            restore = {"lineup": _snapshot_lineup(user_id, team_id), "checkins_on": row.paused_at is None}
            switch = True
            _write_lineup(user_id, team_id, lineup_prefs_for(autonomy.AI))
            _features_on(user_id, now)
            checkins = True
        elif change.autopilot is False and switch:
            switch = False
            if restore:
                # Lineup's preferences go back unless the user put lineup at AI themselves, or changed
                # them since (in Notifications) from what the switch wrote
                if (autonomy.LINEUP not in rows and restore.get("lineup") is not None
                        and _snapshot_lineup(user_id, team_id) == lineup_prefs_for(autonomy.AI)):
                    _write_lineup(user_id, team_id, restore["lineup"])
                # AI needs check-ins: they go back off only when no stored row is at AI
                if not restore.get("checkins_on", True) and not rows and checkins is None:
                    checkins = False
            restore = None

        if change.levels:
            if switch:
                # A row changed while the switch is on: the switch goes off, the rest stay at AI
                rows = {r for r in autonomy.AI_ROWS if autonomy.offers_ai(r)}
                switch, restore = False, None
            for r, level in change.levels.items():
                if level == autonomy.AI:
                    rows.add(r)
                    checkins = True if checkins is None else checkins
                else:
                    rows.discard(r)
                if r == autonomy.LINEUP:
                    _write_lineup(user_id, team_id, lineup_prefs_for(level))

        (Autopilot.update(ai_rows=[r for r in autonomy.AI_ROWS if r in rows], autopilot=switch, restore=restore,
                          updated_at=now)
         .where(Autopilot.team_id == team_id).execute())
        if checkins is True and row.paused_at is not None:
            # Resumed (a pause Dime made itself included): what fell due while it was paused is not missed
            (Autopilot.update(paused_at=None, paused_by=None, paused_reason=None, active_since=now, updated_at=now,
                              user_id=user_id)
             .where(Autopilot.team_id == team_id).execute())
        elif checkins is False and row.paused_at is None:
            (Autopilot.update(paused_at=now, paused_by="user", paused_reason=None, updated_at=now)
             .where(Autopilot.team_id == team_id).execute())
        return Autopilot.get(Autopilot.team_id == team_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)
