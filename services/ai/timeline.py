"""
One team's day on the AI desk's timeline (the Dime desk redesign, step 1).

The desk shows a team's fantasy day (ESPN's, turning at 3 AM ET) as a timeline beside the
chat. This reads that day, and nothing in it writes or spends:

- the check-ins that ran in it, oldest first, each with its calls (holds included), what Dime
  did about each on its own (`action`, services/ai/acting.py), how each move it sent went (the
  move ledger's row for the proposal behind the call, services/move_ledger.py) and the notes its
  own turn wrote (services/ai/memory.py). A check-in is placed by when it ran, so the night
  follow-up about Thursday sits on Friday, when Dime was thinking it;
- the day's scheduled check-ins still to come, while the team's check-ins are on;
- the day's first tip;
- the notes Dime (or the user) set to recheck that day;
- the user's asks due that day that haven't run (services/ai/asks.py; the redesign's step 2): waiting,
  whether or not check-ins are on, or closed without running. One that ran is its check-in (kind ask),
  with the ask beside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from db.base import db_operation
from db.models.ai_conversations import AiMemory
from db.models.autopilot import AiCheckin, AiDecision, Autopilot, AutopilotAction
from db.models.move_outcomes import MoveOutcome
from services import move_ledger, schedule_service
from services.ai import acting, asks, checkins, decisions, memory
from services.scheduled_pickup_service import et_at, rollover_at

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def bounds(day: date) -> tuple[datetime, datetime]:
    """The fantasy day `day` as UTC instants: 3 AM ET on it to 3 AM ET the next day."""
    return rollover_at(day), rollover_at(day + timedelta(days=1))


@dataclass(frozen=True)
class DayRows:
    """What the day's check-ins are made of, read in one go."""

    checkins: list[AiCheckin]
    tallies: dict[int, dict[str, Any]]
    calls: dict[int, list[AiDecision]]
    actions: dict[int, AutopilotAction]
    results: dict[int, MoveOutcome]
    notes: dict[int, list[AiMemory]]
    notes_due: list[AiMemory]
    autopilot: Optional[Autopilot]


def _written_by(note: AiMemory, checkin: AiCheckin, now: datetime) -> bool:
    """Whether the check-in's own turn wrote `note` (not a later follow-up in its conversation). The
    turn's memory tools all run before the check-in is settled, on the same clock, so a note written
    after `finished_at` is a follow-up's, however soon after."""
    at = _aware(note.created_at)
    start = _aware(checkin.started_at)
    end = _aware(checkin.finished_at) or now
    return at is not None and start is not None and start <= at <= end


@db_operation("ai.timeline_day")
def _read(user_id: int, team_id: int, day: date, now: datetime) -> DayRows:
    start, end = bounds(day)
    rows = list(AiCheckin.select()
                .where(AiCheckin.user_id == user_id, AiCheckin.team_id == team_id,
                       AiCheckin.started_at >= start, AiCheckin.started_at < end)
                .order_by(AiCheckin.started_at, AiCheckin.id))
    ids = [r.id for r in rows]
    tallies = checkins._tallies(ids)

    calls: dict[int, list[AiDecision]] = {}
    for d in AiDecision.select().where(AiDecision.checkin_id.in_(ids)).order_by(AiDecision.id) if ids else []:
        calls.setdefault(d.checkin_id_id, []).append(d)
    decision_ids = [d.id for ds in calls.values() for d in ds]
    actions = ({a.decision_id_id: a for a in AutopilotAction.select().where(AutopilotAction.decision_id.in_(decision_ids))}
               if decision_ids else {})

    # How each move Dime sent went: the ledger's row carries the proposal it was attributed to
    proposal_ids = [d.proposal_id_id for ds in calls.values() for d in ds if d.proposal_id_id is not None]
    results: dict[int, MoveOutcome] = {}
    if proposal_ids:
        for o in (MoveOutcome.select()
                  .where(MoveOutcome.team_id == team_id, MoveOutcome.proposal_id.in_(proposal_ids))
                  .order_by(MoveOutcome.nba_date, MoveOutcome.id)):
            results.setdefault(int(o.proposal_id), o)

    by_conversation = {r.conversation_id_id: r for r in rows if r.conversation_id_id is not None}
    notes: dict[int, list[AiMemory]] = {}
    if by_conversation:
        for n in (AiMemory.select()
                  .where(AiMemory.user_id == user_id, AiMemory.conversation_id.in_(list(by_conversation)))
                  .order_by(AiMemory.created_at, AiMemory.id)):
            owner = by_conversation[n.conversation_id_id]
            if _written_by(n, owner, now):
                notes.setdefault(owner.id, []).append(n)

    notes_due = list(AiMemory.select()
                     .where(AiMemory.user_id == user_id, AiMemory.retired_at.is_null(), AiMemory.recheck_on == day,
                            (AiMemory.team_id == team_id) | AiMemory.team_id.is_null())
                     .order_by(AiMemory.created_at, AiMemory.id))
    autopilot = Autopilot.get_or_none(Autopilot.team_id == team_id)
    return DayRows(rows, tallies, calls, actions, results, notes, notes_due, autopilot)


def _result_view(row: MoveOutcome, team_id: int) -> dict[str, Any]:
    """The ledger row as the move-outcomes routes give it (the team's own move, so every field)."""
    q = move_ledger.OutcomeQuery(team_id=team_id, key=None, provider_team_id=None)  # type: ignore[arg-type]
    return move_ledger.outcome_view(row, q)


def _call_view(d: AiDecision, rows: DayRows, team_id: int) -> dict[str, Any]:
    action = rows.actions.get(d.id)
    view = decisions.block_of(d, acting.action_view(action) if action is not None else None)
    result = rows.results.get(d.proposal_id_id) if d.proposal_id_id is not None else None
    # A result only for a move that went out: a held-back chip the user sent later is the user's move
    sent = action is not None and action.outcome == acting.SENT
    view["result"] = _result_view(result, team_id) if result is not None and sent else None
    return view


async def _upcoming(team_id: int, day: date, today: date, autopilot: Optional[Autopilot],
                    now: datetime) -> tuple[list[dict[str, Any]], Optional[datetime]]:
    """The day's scheduled check-ins still to come (none while the team's check-ins are off, and none
    on a past day), and the day's first tip."""
    tips = await checkins._tips([day - timedelta(days=1), day])
    first_tip = et_at(day, tips[day]) if day in tips else None
    if autopilot is None or autopilot.paused_at is not None or day < today:
        return [], first_tip
    current = schedule_service.get_current_matchup(day)
    slots = checkins.slots_for(day, tips.get(day), tips.get(day - timedelta(days=1)),
                               current["start_date"] if current else None)
    taken = await checkins._taken(team_id, sorted({s.nba_date for s in slots}))
    since = _aware(autopilot.active_since) or now
    out = [{"kind": s.kind, "date": s.nba_date, "due_at": s.due_at} for s in slots
           if (s.kind, s.nba_date) not in taken and s.closes_at > now and s.closes_at > since]
    return out, first_tip


async def day(user_id: int, team_id: int, day: Optional[date] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """The team's fantasy day `day` (today's when left out) for the timeline (an `AiTimelineData`).
    The caller checks access and that the team is the user's."""
    now = now or _now()
    today = checkins.fantasy_day(now)
    day = day or today
    rows = await _read(user_id, team_id, day, now)
    upcoming, first_tip = await _upcoming(team_id, day, today, rows.autopilot, now)
    start, end = bounds(day)
    ran = await asks.of_checkins([r.id for r in rows.checkins if r.kind == checkins.ASK])
    items = []
    for r in rows.checkins:
        view = checkins.summary_of(r, rows.tallies.get(r.id))
        view["calls"] = [_call_view(d, rows, team_id) for d in rows.calls.get(r.id, [])]
        view["notes"] = [memory.note_view(n) for n in rows.notes.get(r.id, [])]
        view["ask"] = ran.get(r.id)
        items.append(view)
    return {
        "team_id": team_id,
        "date": day,
        "today": today,
        "starts_at": start,
        "ends_at": end,
        "checkins_on": rows.autopilot is not None and rows.autopilot.paused_at is None,
        "first_tip": first_tip,
        "checkins": items,
        "upcoming": upcoming,
        "notes_due": [memory.note_view(n) for n in rows.notes_due],
        "asks": await asks.of_day(user_id, team_id, start, end),
    }
