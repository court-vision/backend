"""
`get_automations`: what the team's automations are set to do, for Dime to read
before they act (the Dime autopilot plan, step 3, "Looking before they act").

Three automations run a Court Vision team without the user:

- **Auto-lineup** (data-platform's lineup alerts, the backend's fill planner):
  once a day, in the team's window before the first tip, the server's fill is
  written to ESPN (auto-lineup on) or emailed (alerts only). The tool says
  whether it runs for the team, when its window opens today, and what it wrote
  today (usr.roster_moves, source auto), plus any late-scratch swap the
  availability check made (source availability). What it would fill *now* is
  get_lineup's `fill`, the same planner.
- **Stream lanes**: each open lane, the spot it streams, when it decides next and
  for which day, and its plan (who holds the spot each day, what each adds, the
  gain over holding).
- **Scheduled pickups** still pending, each with its pickup_id (cancel_pickup's).
- **Holds** (step 4, only while steering is on): Dime's live holds, with the
  reason each was set and its hold_id (steer's clear).
- **Autopilot** (step 5, only on a team where Dime acts on some automation):
  the automations it acts on, the policy its own sends are held to, the adds it
  has used of its caps, and what it did lately (services/ai/acting.py).

DB reads only, no ESPN call. NBA player ids only, as every tool result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from core.nba_calendar import EASTERN
from db.base import run_db
from db.models.nba.games import Game
from db.models.nba.players import Player
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from services import schedule_service, steering
from services.ai import steering as ai_steering
from services.lineup_editor_service import _slot_name
from services.scheduled_pickup_service import et_at
from services.stream_lane_service import StreamLaneService

GET_AUTOMATIONS_NAME = "get_automations"
# The fill's window closes this long before the first tip (data-platform's lineup alerts)
FILL_WINDOW_CLOSES = timedelta(minutes=15)


class GetAutomationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)


GET_AUTOMATIONS: dict[str, Any] = {
    "name": GET_AUTOMATIONS_NAME,
    "description": (
        "What the team's automations are set to do, read before they act. auto_lineup: whether "
        "Court Vision fills this team's lineup each game day (applies true: it writes the fill to ESPN; "
        "false: it only emails the plan), when today's window opens (ET), and what it wrote today, "
        "if it has run (each move: player, from and to slot). What it would fill now is get_lineup's "
        "`fill`, the same planner. late_scratch: any swap the injury check made today. lanes: each open "
        "stream lane, the player holding its spot, when it decides next and for which day, and its "
        "plan (who holds the spot each day, the value each adds, the gain over keeping the spot's "
        "player, adds it uses). pickups: scheduled pickups still pending (day, add, drop, whether a "
        "lane made it). Read it before judging what the automations will do."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"team_id": {"type": "integer", "description": "One of the user's team_ids"}},
        "required": ["team_id"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class FillSetting:
    """Auto-lineup for one team: `runs` (the fill is planned for it each game day), `applies` (and
    written to ESPN), `minutes_before` (the window opens this long before the first tip)."""

    runs: bool
    applies: bool
    minutes_before: int


def fill_setting(prefs: Any, team_pref: Any) -> FillSetting:
    """The data-platform pipeline's rule, read the same way: only a user with an account-level
    preference row whose alerts are on is processed at all; a team override wins where it is set."""
    if prefs is None or not prefs.lineup_alerts_enabled:
        return FillSetting(False, False, 0)

    def pick(field: str) -> Any:
        value = getattr(team_pref, field, None) if team_pref is not None else None
        return value if value is not None else getattr(prefs, field)
    runs = pick("lineup_alerts_enabled") is not False
    return FillSetting(runs, runs and bool(pick("auto_lineup_enabled")), int(pick("alert_minutes_before") or 0))


def _prefs(user_id: int, team_id: int) -> tuple[Any, Any]:
    prefs = NotificationPreference.get_or_none(NotificationPreference.user == user_id)
    team_pref = (NotificationTeamPreference.select()
                 .where((NotificationTeamPreference.user == user_id) & (NotificationTeamPreference.team_id == team_id))
                 .first())
    return prefs, team_pref


def _todays_writes(team_id: int, today: date) -> list[Any]:
    return list(RosterMove.select()
                .where((RosterMove.team == team_id) & (RosterMove.nba_date == today)
                       & (RosterMove.source.in_(["auto", "availability"])))
                .order_by(RosterMove.id))


def _pending_pickups(team_id: int) -> list[Any]:
    return list(ScheduledPickup.select()
                .where((ScheduledPickup.team == team_id) & (ScheduledPickup.status == "pending"))
                .order_by(ScheduledPickup.nba_date, ScheduledPickup.id))


def _names(espn_ids: list[int]) -> dict[int, tuple[Optional[int], str, Optional[str]]]:
    """ESPN id -> (NBA id, name, team) for the players Court Vision knows (nba.players has no
    team; the rows that carry one say it themselves)."""
    if not espn_ids:
        return {}
    rows = Player.select(Player.id, Player.espn_id, Player.name).where(Player.espn_id.in_(espn_ids))
    return {r.espn_id: (r.id, r.name, None) for r in rows}


def _player(espn_id: Optional[int], names: dict, name: Optional[str] = None, team: Optional[str] = None,
            nba_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    if espn_id is None:
        return None
    known = names.get(espn_id)
    return {"player_id": nba_id or (known[0] if known else None),
            "name": name or (known[1] if known else str(espn_id)),
            "team": team or (known[2] if known else None)}


def _write_view(row: Any, names: dict) -> dict[str, Any]:
    moves = []
    for m in row.moves or []:
        who = _player(m.get("player_id"), names, m.get("name"))
        if row.kind == "transaction":
            moves.append({**(who or {}), "action": m.get("action")})
        else:
            moves.append({**(who or {}), "from_slot": _slot_name(m.get("from_slot_id")),
                          "to_slot": _slot_name(m.get("to_slot_id")), "role": m.get("role"), "note": m.get("note")})
    return {"status": row.status, "at": row.created_at.isoformat() if row.created_at else None, "moves": moves}


def _hhmm(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(EASTERN).strftime("%H:%M") if moment else None


def _lane_view(lane: Any, names: dict) -> dict[str, Any]:
    p = lane.projection

    def who(lp: Any) -> Optional[dict[str, Any]]:
        return _player(lp.player_id, names, lp.name, lp.team, lp.nba_player_id) if lp is not None else None
    return {
        "lane_id": lane.id,
        "status": lane.status,
        "reason": lane.reason,
        "spot_holder": who(lane.holder),
        "started_with": who(lane.original),
        "ends": lane.end_date.isoformat(),
        "next_decision_at_et": (lane.next_decision_at.astimezone(EASTERN).strftime("%a %H:%M")
                                if lane.next_decision_at else None),
        "next_day": _period_date(lane, lane.next_period),
        "plan": None if p is None else {
            "as_of": p.as_of.isoformat(),
            "days": [{"date": d.nba_date.isoformat(), "holder": who(d.player), "value": d.value, "switch": d.switch}
                     for d in p.days],
            "value": p.value, "hold_value": p.hold_value, "gain": p.gain,
            "adds": p.adds, "adds_left": p.adds_left, "lanes_planned_together": p.lanes,
        },
    }


def _period_date(lane: Any, period: Optional[int]) -> Optional[str]:
    """The date an ESPN day of the lane falls on, counted back from its last day."""
    if period is None:
        return None
    return (lane.end_date - timedelta(days=lane.end_period - period)).isoformat()


def _first_tip(d: date):
    return Game.get_earliest_game_time_on_date(d)


async def get_automations(team: Any, user_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = schedule_service.get_nba_today()
    prefs, team_pref = await run_db("ai.automations_prefs", _prefs, user_id, team.team_id)
    setting = fill_setting(prefs, team_pref)
    tip = await run_db("ai.automations_first_tip", _first_tip, today)
    writes = await run_db("ai.automations_writes", _todays_writes, team.team_id, today)
    pickups = await run_db("ai.automations_pickups", _pending_pickups, team.team_id)
    lanes = (await StreamLaneService.list_for_team(team)).data
    open_lanes = list(lanes.open) if lanes else []

    espn_ids: set[int] = set()
    for w in writes:
        espn_ids |= {m.get("player_id") for m in (w.moves or []) if m.get("player_id")}
    for p in pickups:
        espn_ids |= {pid for pid in (p.add_player_id, p.drop_player_id) if pid}
    for lane in open_lanes:
        espn_ids |= {lane.holder.player_id, lane.original.player_id}
        if lane.projection:
            espn_ids |= {d.player.player_id for d in lane.projection.days}
    names = await run_db("ai.automations_names", _names, sorted(espn_ids))

    fill_at = None
    if tip is not None and setting.runs:
        tip_at = et_at(today, tip)
        fill_at = {"opens_et": _hhmm(tip_at - timedelta(minutes=setting.minutes_before)),
                   "closes_et": _hhmm(tip_at - FILL_WINDOW_CLOSES)}
    auto = [w for w in writes if w.source == "auto"]
    late = [w for w in writes if w.source == "availability"]
    notes = []
    if not setting.runs:
        notes.append("Auto-lineup doesn't run for this team: its lineup alerts are off.")
    elif tip is None:
        notes.append("No games today, so auto-lineup has nothing to fill.")
    return {
        "team_id": team.team_id,
        "today": today.isoformat(),
        "first_tip_et": tip.strftime("%H:%M") if tip is not None else None,
        "auto_lineup": {
            "runs": setting.runs,
            "applies": setting.applies,
            "window": fill_at,
            "today": [_write_view(w, names) for w in auto] or None,
        },
        "late_scratch": [_write_view(w, names) for w in late],
        "lanes": [_lane_view(lane, names) for lane in open_lanes],
        "pickups": [{
            "pickup_id": p.id,
            "date": p.nba_date.isoformat(),
            "add": _player(p.add_player_id, names, p.add_name, p.add_team),
            "drop": _player(p.drop_player_id, names, p.drop_name, p.drop_team),
            "from_stream_lane": p.lane_id is not None,
            **({"scheduled_by_you": True} if getattr(p, "made_by_autopilot", False) else {}),
        } for p in pickups],
        **({"holds": await _holds(team.team_id, today)} if steering.enabled() else {}),
        **await _autopilot(team.team_id),
        "notes": notes,
    }


async def _autopilot(team_id: int) -> dict[str, Any]:
    """What Dime acts on here and what it has used of the policy (step 5), under `autopilot`; nothing
    on a team where Dime acts on no automation, so the result is step 3's there."""
    from services.ai import acting, autonomy
    try:
        row = await acting._autopilot_row(team_id)
        if not autonomy.acting_rows(row):
            return {}
        return {"autopilot": await acting.policy_view(team_id, row)}
    except Exception:
        return {}


async def _holds(team_id: int, today: date) -> list[dict[str, Any]]:
    """Dime's live holds on the team (services/steering.py), with their reasons and hold_ids for
    `steer`'s clear. Only while steering is on, so the result is as before when it is off."""
    try:
        rows = await steering.live_rows(team_id, today)
    except Exception:
        return []
    return [ai_steering.hold_view(r) for r in rows]
