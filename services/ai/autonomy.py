"""
Which automations Dime does itself on a team (usr.autopilot, migration 0044; the Dime
autopilot plan's "Automation levels", step 5). Pure: no reads, no writes.

Each automation has one level on a team: off, notify (an email says what is due), act
(Court Vision does it, no AI) or ai (Dime does it: it reviews the automation's plan,
acts within the policy, and logs every call and move). The automations, the table's
rows:

    lineup         each day's slots: the tip-off email (notify), auto-lineup's fill (act);
                   at AI Dime reviews the fill before lock and moves slots or pins players
    daily_actions  the Week desk's list of IR moves and the day's pickup and drop; at AI
                   Dime makes its own adds and drops and may cancel a pickup the user
                   scheduled. Its notify and act aren't built yet
    lanes          stream lanes, which run as the user set them (act); at AI Dime also
                   starts, pauses, stops and steers them
    late_scratch   late scratches and news: an email with the swap a ruling calls for
                   (notify), Court Vision making that swap (act; services/late_scratch_service.py);
                   at AI an event check-in before the player's game locks (step 6,
                   services/ai/events.py)
    checkins       Dime's check-ins: off, or ai while the team isn't paused

Below AI a level lives with its automation (lineup's and late scratches' in the
notification preferences, services/ai/levels.py; lanes are always act), so what is
stored here is the AI part:
`ai_rows`, and the `autopilot` switch, which puts every row that offers AI at AI while
it is on (off, the stored rows count again).

Dime sends a move itself (`acts`) only when its row is at AI, check-ins are on and
DIME_ACTING_ENABLED is on. Otherwise a check-in's change is a review: logged as what
Dime would have done and never sent (step 3's shadow). A move counts under the row of
its kind (`rows_of`): slot moves and pins under lineup, adds and drops under
daily_actions, lanes and their holds under lanes, and whatever an event check-in
commits under late_scratch. A chip that touches two rows acts only if both do.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from core.settings import settings

# Levels, least to most
OFF, NOTIFY, ACT, AI = "off", "notify", "act", "ai"
LEVELS = (OFF, NOTIFY, ACT, AI)

# What became of a check-in's change: logged as a review, or handled by Dime at AI
REVIEW = "review"

# The table's rows; the first four can be at AI (usr.autopilot.ai_rows)
LINEUP, DAILY_ACTIONS, LANES, LATE_SCRATCH, CHECKINS = "lineup", "daily_actions", "lanes", "late_scratch", "checkins"
AI_ROWS = (LINEUP, DAILY_ACTIONS, LANES, LATE_SCRATCH)
ROWS = (*AI_ROWS, CHECKINS)
LABELS = {LINEUP: "Lineup", DAILY_ACTIONS: "Daily actions", LANES: "Stream lanes",
          LATE_SCRATCH: "Late scratches and news", CHECKINS: "Check-ins"}

# What each row doesn't offer, and why (a level not listed is offered)
NOT_OFFERED: dict[str, dict[str, str]] = {
    LINEUP: {},
    DAILY_ACTIONS: {
        NOTIFY: "Not built yet: a morning email with the day's list",
        ACT: "Not built yet: Court Vision making the list's moves",
    },
    LANES: {
        OFF: "Lanes are started and stopped one at a time in the Week desk",
        NOTIFY: "Not offered yet",
    },
    LATE_SCRATCH: {},
    CHECKINS: {
        NOTIFY: "Check-ins are Dime's own",
        ACT: "Check-ins are Dime's own",
    },
}

# Check-in kinds that are events (step 6; checkins.EVENT): what they commit counts under late_scratch
EVENT_KINDS: frozenset[str] = frozenset({"event"})


def acting_available() -> bool:
    """The server lets Dime send anything itself (DIME_ACTING_ENABLED)."""
    return settings.dime_acting_enabled


def events_available() -> bool:
    """The server starts event check-ins (DIME_EVENTS_ENABLED; services/ai/events.py)."""
    return settings.dime_events_enabled


def offered(row: str, level: str) -> Optional[str]:
    """None when the row offers the level; else why not."""
    return NOT_OFFERED.get(row, {}).get(level)


def offers_ai(row: str) -> bool:
    return row in AI_ROWS and offered(row, AI) is None


def stored(autopilot: Any) -> frozenset[str]:
    """The rows the user put at AI themselves (the switch aside)."""
    return frozenset(r for r in (getattr(autopilot, "ai_rows", None) or []) if r in AI_ROWS)


def switch_on(autopilot: Any) -> bool:
    return bool(getattr(autopilot, "autopilot", False)) if autopilot is not None else False


def at_ai(autopilot: Any, row: str) -> bool:
    """The row is at AI on the team: the autopilot switch is on, or the user put it there."""
    if autopilot is None or not offers_ai(row):
        return False
    return switch_on(autopilot) or row in stored(autopilot)


def ai_rows(autopilot: Any) -> list[str]:
    """Every row at AI now, in the table's order."""
    return [r for r in AI_ROWS if at_ai(autopilot, r)]


def checkins_on(autopilot: Any) -> bool:
    return autopilot is not None and getattr(autopilot, "paused_at", None) is None


def acts(autopilot: Any, row: str) -> bool:
    """Dime sends this row's moves itself: at AI, check-ins on, and acting on on the server."""
    return at_ai(autopilot, row) and checkins_on(autopilot) and acting_available()


def acting_rows(autopilot: Any) -> list[str]:
    """The rows Dime acts on now; empty means every change a check-in logs is a review."""
    return [r for r in AI_ROWS if acts(autopilot, r)]


def is_event(checkin_kind: Optional[str]) -> bool:
    return checkin_kind in EVENT_KINDS


# ------------------------------- what a chip touches ------------------------------- #


def _hold_rows(hold: Mapping[str, Any]) -> set[str]:
    kind = hold.get("kind")
    if kind == "pin":
        return {LINEUP}
    if kind == "override" and (hold.get("share") or 0) <= 0:
        return {LINEUP, LANES}           # the lanes plan without him, and the fill reads him as out
    return {LANES}


def rows_of(proposal: Any, *, event: bool = False) -> frozenset[str]:
    """The rows a proposal counts under: its kind's, or late_scratch for an event check-in's."""
    if event:
        return frozenset({LATE_SCRATCH})
    kind = proposal.kind
    action = proposal.action or {}
    if kind == "lineup":
        return frozenset({LINEUP})
    if kind in ("transaction", "scheduled"):
        return frozenset({DAILY_ACTIONS})
    if kind == "lane":
        return frozenset({LANES})
    if kind == "cancel_pickup":
        return frozenset({LANES if action.get("lane_id") else DAILY_ACTIONS})
    if kind == "steer":
        rows: set[str] = set()
        for h in [*(action.get("holds") or []), *(action.get("clear") or [])]:
            rows |= _hold_rows(h)
        return frozenset(rows or {LANES})
    return frozenset({DAILY_ACTIONS})


def chip_acts(autopilot: Any, proposal: Any, *, event: bool = False) -> bool:
    """Dime sends this proposal itself: every row it touches acts."""
    return all(acts(autopilot, r) for r in rows_of(proposal, event=event))


def primary(rows: Iterable[str]) -> str:
    """One row to file an action under: the first in the table's order."""
    have = set(rows)
    return next(r for r in AI_ROWS if r in have)


def adds_of(proposal: Any) -> int:
    """Players the move adds: what the caps count (a transaction's or a scheduled pickup's add)."""
    return 1 if proposal.kind in ("transaction", "scheduled") and proposal.add_player_id is not None else 0


def words(rows: Iterable[str]) -> str:
    """'lineup, daily actions (your own adds and drops)', for the check-in's brief."""
    say = {LINEUP: "lineup (slot moves and pins)", DAILY_ACTIONS: "daily actions (your own adds and drops, and "
           "cancelling the user's scheduled pickups)", LANES: "stream lanes (starting, pausing, stopping and "
           "steering them)", LATE_SCRATCH: "late scratches (what an event check-in does)"}
    return ", ".join(say[r] for r in rows)


# ------------------------------- the table ------------------------------- #


def level_of(autopilot: Any, row: str, lineup_below_ai: str, late_scratch_below_ai: str = OFF) -> str:
    """The row's level now. Below AI, lineup's and late scratches' come from the notification
    preferences (`lineup_below_ai`, `late_scratch_below_ai`), lanes are act, and the rest are off."""
    if row == CHECKINS:
        return AI if checkins_on(autopilot) else OFF
    if at_ai(autopilot, row):
        return AI
    if row == LINEUP:
        return lineup_below_ai
    if row == LATE_SCRATCH:
        return late_scratch_below_ai
    if row == LANES:
        return ACT
    return OFF


def table(autopilot: Any, *, lineup_below_ai: str, lineup_email: bool,
          late_scratch_below_ai: str = OFF) -> list[dict[str, Any]]:
    """The team's automations table, as the API returns it: each row's level, each level's
    availability and why not, and a note when the row does less than its level says.
    `lineup_email` is whether the account's lineup email is on: without it no team's lineup
    runs, so lineup's notify, act and AI can't be picked."""
    switch = switch_on(autopilot)
    paused = not checkins_on(autopilot)
    out = []
    for row in ROWS:
        level = level_of(autopilot, row, lineup_below_ai, late_scratch_below_ai)
        options = []
        for lv in LEVELS:
            reason = offered(row, lv)
            if reason is None and row == LINEUP and lv != OFF and not lineup_email:
                reason = "Your account's lineup email is off, so no team's lineup runs: turn it on in Notifications"
            options.append({"level": lv, "available": reason is None, "reason": reason})
        note = None
        if row != CHECKINS and level == AI:
            if switch:
                note = "Autopilot sets these"
            if paused:
                note = "Paused: Dime does nothing here until you resume check-ins"
            elif row == LATE_SCRATCH and not events_available():
                note = "Nothing starts yet: event check-ins are switched off on the server"
            elif not acting_available():
                note = "Reviews only: Dime's acting is switched off on the server"
            if row == LINEUP and not lineup_email:
                note = (f"{note}. " if note else "") + "Auto-lineup doesn't run: your account's lineup email is off"
        if row == CHECKINS and paused and getattr(autopilot, "paused_by", None) == "system":
            note = f"Dime paused itself: {autopilot.paused_reason}" if autopilot.paused_reason else "Dime paused itself"
        out.append({"row": row, "label": LABELS[row], "level": level, "options": options, "note": note})
    return out
