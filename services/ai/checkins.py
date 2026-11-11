"""
Dime's check-ins, in shadow (usr.autopilot / usr.ai_checkins, migration 0042;
the Dime autopilot plan, step 3).

A check-in is a turn Dime starts itself. Four kinds, each about one fantasy day
(US Eastern, turning at 3 AM like ESPN's):

- **morning**, 9 AM ET on a day of the season: what the automations will do
  today (auto-lineup's fill, the lanes' next pickups, the scheduled pickups),
  judged one by one, and any move none of them makes;
- **weekly**, the morning check-in of a matchup's first day, with the week's
  preview (the opponent, the race) and a retro of last week's calls;
- **pre_lock**, shortly before the day's first tip: the lineup after the fill and
  the late-scratch check, one call on it;
- **night**, 6 AM ET the next day, once the box scores and the nightly move
  ledger are in: how the day's calls went, and what that teaches;
- **event**, when a verdict changes or news lands about a player on the roster
  (services/ai/events.py; the plan's step 6): not on the clock, and not a slot,
  so a team can have several a day, within their own cap.

Each runs once per team, kind and day (claimed by inserting its row), inside a
window; a window that closes before the check-in could run leaves it `missed`
(never one that closed before the team's autopilot was switched on). The cron
job (cron-runner `dime-checkins` → data-platform → POST /v1/internal/jobs/
checkins/run) ticks every fifteen minutes and starts what is due; the user can
also run one now from the AI desk.

A check-in is an ordinary agent turn (services/ai/agent.py) with its own
conversation (kind `checkin`), the server's brief as its message, the check-in
tool list (`log_decision` added) and bounds, and its spend on the autopilot line
(services/ai/spend.py). In shadow nothing reaches ESPN: its proposals are stored
`shadow`, and every call it makes is a decision (services/ai/decisions.py) the
user marks agree or disagree.

What Dime may do itself on the team is its automation levels (services/ai/autonomy.py;
the plan's step 5): while no automation Dime acts on is at AI (the default) a check-in
is step 3's shadow; otherwise its brief says which automations are at AI, and a change
it logs on them is sent (services/ai/acting.py). The Check-ins row is this switch: on while the team isn't paused.

Every check-in takes the team's pending events when it starts (events.take), so
a scheduled one about to run lists them in its brief rather than another turn
starting for them. Events come in only for a team whose Late scratches row is at
AI (or with autopilot on); anywhere else there are none.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from peewee import fn

from core.errors import AppError, AuthorizationError, ConflictError, NotFoundError, ServiceUnavailableError
from core.logging import get_logger
from core.nba_calendar import EASTERN
from core.settings import settings
from db.base import db_operation
from db.models.autopilot import AiCheckin, AiDecision, Autopilot, AutopilotAction
from db.models.nba.games import FANTASY_SEASON_TYPES, Game
from db.models.teams import Team
from schemas.common import ApiStatus
from services import schedule_service
from services.ai import agent, autonomy, conversations, levels, spend
from services.scheduled_pickup_service import et_at

MORNING, WEEKLY, PRE_LOCK, NIGHT = "morning", "weekly", "pre_lock", "night"
# What the schedule (and Run now) can start; an event check-in starts only for its events
KINDS = (MORNING, WEEKLY, PRE_LOCK, NIGHT)
EVENT = "event"
RUNNING, DONE, FAILED, MISSED, SKIPPED = "running", "done", "failed", "missed", "skipped"
# States that count toward the day's cap: a check-in that ran (or is running)
_RAN = (RUNNING, DONE, FAILED)

# The windows, in Eastern time
MORNING_AT, MORNING_CLOSES_AT = time(9), time(14)
# Pre-lock runs after the fill has written the day's lineup (its window opens well before
# the tip and closes a quarter hour before it) and before the tip locks the first players
PRE_LOCK_LEAD, PRE_LOCK_CLOSES = timedelta(minutes=45), timedelta(minutes=10)
# The night follow-up waits for the nightly move ledger (rebuilt in the small hours)
NIGHT_AT, NIGHT_CLOSES_AT = time(6), time(12)
# A manual run before this long ahead of the first tip is a morning one; after it, pre-lock
_MANUAL_PRE_LOCK = timedelta(hours=2)
# A check-in still `running` this long past its turn's own deadline died with its process
_STALE_MARGIN = timedelta(minutes=10)

LABELS = {MORNING: "Morning check-in", WEEKLY: "Weekly check-in", PRE_LOCK: "Pre-lock check-in",
          NIGHT: "Night follow-up", EVENT: "Event check-in"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fantasy_day(now: datetime) -> date:
    """The ESPN fantasy day at `now`: before 3 AM ET it is still yesterday (schedule_service)."""
    et = now.astimezone(EASTERN)
    return (et - timedelta(days=1)).date() if et.time() < schedule_service.FANTASY_DAY_ROLLOVER_ET else et.date()


# ------------------------------- the schedule ------------------------------- #


@dataclass(frozen=True)
class Slot:
    """One check-in a team is owed: its kind, the day it is about, and its window (UTC)."""

    kind: str
    nba_date: date
    due_at: datetime
    closes_at: datetime


def slots_for(today: date, tip_today: Optional[time], tip_yesterday: Optional[time],
              matchup_start: Optional[date]) -> list[Slot]:
    """The check-ins of the fantasy day `today`, in the order they fall due: the night follow-up
    on yesterday's games, the morning (weekly on a matchup's first day), and pre-lock. Morning
    needs the season (`matchup_start` is the current matchup's first day), the other two a day
    with games; morning closes before pre-lock opens."""
    out: list[Slot] = []
    if tip_yesterday is not None:
        out.append(Slot(NIGHT, today - timedelta(days=1), et_at(today, NIGHT_AT), et_at(today, NIGHT_CLOSES_AT)))
    tip = et_at(today, tip_today) if tip_today is not None else None
    if matchup_start is not None:
        due, closes = et_at(today, MORNING_AT), et_at(today, MORNING_CLOSES_AT)
        if tip is not None:
            closes = min(closes, tip - PRE_LOCK_LEAD)
        if closes > due:
            out.append(Slot(WEEKLY if matchup_start == today else MORNING, today, due, closes))
    if tip is not None:
        out.append(Slot(PRE_LOCK, today, tip - PRE_LOCK_LEAD, tip - PRE_LOCK_CLOSES))
    return sorted(out, key=lambda s: s.due_at)


def standing(slot: Slot, now: datetime, active_since: datetime) -> Optional[str]:
    """`due` inside its window, `missed` past it, None before it opens or when it closed before the
    team's autopilot was switched on (or resumed)."""
    if now < slot.due_at or slot.closes_at <= active_since:
        return None
    return "due" if now < slot.closes_at else "missed"


def default_kind(now: datetime, today: date, tip_today: Optional[time], matchup_start: Optional[date]) -> str:
    """What a manual run is when the user doesn't say: by the clock against today's first tip."""
    if tip_today is not None:
        tip = et_at(today, tip_today)
        if now >= tip:
            return NIGHT
        if now >= tip - _MANUAL_PRE_LOCK:
            return PRE_LOCK
    return WEEKLY if matchup_start == today else MORNING


def day_of(kind: str, today: date) -> date:
    """The day a check-in of `kind` started today is about: the night follow-up is yesterday's."""
    return today - timedelta(days=1) if kind == NIGHT else today


@dataclass(frozen=True)
class Calendar:
    """What the schedule needs about the days around `today`."""

    today: date
    tips: dict[date, time]
    matchup_start: Optional[date]

    def slots(self, day: Optional[date] = None) -> list[Slot]:
        d = day or self.today
        start = self.matchup_start
        if d != self.today:
            current = schedule_service.get_current_matchup(d)
            start = current["start_date"] if current else None
        return slots_for(d, self.tips.get(d), self.tips.get(d - timedelta(days=1)), start)


@db_operation("ai.checkin_tips")
def _tips(days: list[date]) -> dict[date, time]:
    rows = (Game.select(Game.game_date, fn.MIN(Game.start_time_et))
            .where(Game.game_date.in_(days), Game.start_time_et.is_null(False),
                   # Fantasy games only: no check-in on an All-Star, preseason or playoff day. A game not
                   # yet labelled counts, as Game.in_season and PlayerGameStats.for_fantasy() keep it.
                   Game.season_type.in_(FANTASY_SEASON_TYPES) | Game.season_type.is_null())
            .group_by(Game.game_date)
            .tuples())
    return {d: t for d, t in rows if t is not None}


async def calendar(now: datetime) -> Calendar:
    today = fantasy_day(now)
    tips = await _tips([today + timedelta(days=n) for n in (-2, -1, 0, 1)])
    current = schedule_service.get_current_matchup(today)
    return Calendar(today=today, tips=tips, matchup_start=current["start_date"] if current else None)


# ------------------------------- the rows ------------------------------- #


def allowed(team_id: int, user_id: int) -> bool:
    """A team Dime may run check-ins on: on AUTOPILOT_TEAM_IDS, its owner on the AI allowlist."""
    return team_id in settings.autopilot_team_ids and user_id in settings.ai_agent_user_ids


@db_operation("ai.autopilot_live")
def _live() -> list[Autopilot]:
    """The switched-on teams whose owner still owns them."""
    return list(Autopilot.select(Autopilot)
                .join(Team, on=((Autopilot.team_id == Team.team_id) & (Autopilot.user_id == Team.user_id)))
                .where(Autopilot.paused_at.is_null()))


@db_operation("ai.checkin_taken")
def _taken(team_id: int, days: list[date]) -> set[tuple[str, date]]:
    """The slots of `days` this team already has a row for (run, missed or skipped)."""
    return set(AiCheckin.select(AiCheckin.kind, AiCheckin.nba_date)
               .where(AiCheckin.team_id == team_id, AiCheckin.nba_date.in_(days), AiCheckin.manual == False)  # noqa: E712
               .tuples())


@db_operation("ai.checkin_busy")
def _busy(team_id: int) -> bool:
    return AiCheckin.select().where(AiCheckin.team_id == team_id, AiCheckin.state == RUNNING).exists()


@db_operation("ai.checkin_ran_since")
def _ran_since(team_id: int, since: datetime, *, scheduled_only: bool = False) -> int:
    """Check-ins that ran (or are running) on the team since `since`; with `scheduled_only`, not
    counting the ones the user ran from the desk."""
    query = AiCheckin.select().where(AiCheckin.team_id == team_id, AiCheckin.started_at >= since,
                                     AiCheckin.state.in_(_RAN))
    if scheduled_only:
        query = query.where(AiCheckin.manual == False)  # noqa: E712
    return query.count()


@db_operation("ai.checkin_run_times")
def _run_times(team_id: int, since: datetime) -> list[datetime]:
    """When each check-in Dime started itself since `since` (desk runs don't count, as in the tick):
    an event kick weighs which of them are still inside the day before each check-in it must leave
    room for."""
    return [at for (at,) in AiCheckin.select(AiCheckin.started_at)
            .where(AiCheckin.team_id == team_id, AiCheckin.started_at >= since, AiCheckin.state.in_(_RAN),
                   AiCheckin.manual == False)  # noqa: E712
            .tuples()]


@db_operation("ai.checkin_claim")
def _claim(team_id: int, user_id: int, kind: str, nba_date: date, due_at: datetime, *, manual: bool,
           now: datetime, state: str = RUNNING, reason: Optional[str] = None) -> Optional[int]:
    """Insert the check-in's row; None when a scheduled one already has its slot (another tick
    claimed it), or when the team already has one running (the unique index makes that check atomic,
    whatever an earlier read said). A manual run is outside the one-per-slot rule only."""
    return (AiCheckin.insert(team_id=team_id, user_id=user_id, kind=kind, nba_date=nba_date, manual=manual,
                             due_at=due_at, state=state, reason=reason, started_at=now,
                             finished_at=None if state == RUNNING else now)
            .on_conflict_ignore()
            .execute())


@db_operation("ai.checkin_attach")
def _attach(checkin_id: int, conversation_id: int) -> None:
    AiCheckin.update(conversation_id=conversation_id).where(AiCheckin.id == checkin_id).execute()


@db_operation("ai.checkin_settle")
def _settle(checkin_id: int, state: str, reason: Optional[str], turn_id: Optional[int], cost: int,
            now: datetime) -> None:
    """Close a check-in with its turn. One the sweep already called stale is closed all the same:
    the turn finished after all."""
    (AiCheckin.update(state=state, reason=reason, turn_id=turn_id, cost_micros=cost, finished_at=now)
     .where(AiCheckin.id == checkin_id, AiCheckin.state.in_([RUNNING, FAILED]))
     .execute())


@db_operation("ai.checkin_sweep")
def _sweep(before: datetime, now: datetime) -> int:
    return (AiCheckin.update(state=FAILED, reason="stale", finished_at=now)
            .where(AiCheckin.state == RUNNING, AiCheckin.started_at < before)
            .execute())


@db_operation("ai.checkin_get")
def _get(checkin_id: int) -> Optional[AiCheckin]:
    return AiCheckin.get_or_none(AiCheckin.id == checkin_id)


@db_operation("ai.checkin_of_conversation")
def _of_conversation(conversation_id: int, user_id: int) -> Optional[AiCheckin]:
    return (AiCheckin.select()
            .where(AiCheckin.conversation_id == conversation_id, AiCheckin.user_id == user_id)
            .first())


@db_operation("ai.checkins_list")
def _list(user_id: int, team_id: Optional[int], limit: int) -> tuple[list[AiCheckin], dict[int, dict[str, Any]]]:
    query = AiCheckin.select().where(AiCheckin.user_id == user_id)
    if team_id is not None:
        query = query.where(AiCheckin.team_id == team_id)
    rows = list(query.order_by(AiCheckin.started_at.desc(), AiCheckin.id.desc()).limit(limit))
    return rows, _tallies([r.id for r in rows])


def _tallies(checkin_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    if not checkin_ids:
        return out
    for d in (AiDecision.select(AiDecision.checkin_id, AiDecision.call, AiDecision.verdict)
              .where(AiDecision.checkin_id.in_(checkin_ids))):
        t = out.setdefault(d.checkin_id_id, {"decisions": 0, "changes": 0, "agree": 0, "disagree": 0, "sent": 0})
        t["decisions"] += 1
        t["changes"] += d.call == "change"
        if d.verdict in ("agree", "disagree"):
            t[d.verdict] += 1
    # The moves it sent on its own (the acting step)
    for cid, n in (AutopilotAction.select(AutopilotAction.checkin_id, fn.COUNT(AutopilotAction.id))
                   .where(AutopilotAction.checkin_id.in_(checkin_ids), AutopilotAction.outcome == "sent")
                   .group_by(AutopilotAction.checkin_id).tuples()):
        if cid in out:
            out[cid]["sent"] = int(n)
    # What it was told came in (the events step), on the tally under `events`
    from services.ai import events
    for cid, rows in events.of_checkins(checkin_ids).items():
        out.setdefault(cid, {"decisions": 0, "changes": 0, "agree": 0, "disagree": 0, "sent": 0})
        out[cid]["events"] = [events.event_view(e) for e in rows]
    return out


@db_operation("ai.checkin_tally")
def _tally(checkin_id: int) -> dict[str, Any]:
    return _tallies([checkin_id]).get(checkin_id, {})


@db_operation("ai.autopilot_teams")
def _autopilot_teams(user_id: int, team_ids: list[int]) -> list[tuple[int, Optional[Autopilot]]]:
    """The user's teams among `team_ids`, each with its autopilot row (None when never switched on)."""
    if not team_ids:
        return []
    owned = [t for (t,) in Team.select(Team.team_id)
             .where(Team.user_id == user_id, Team.team_id.in_(team_ids))
             .order_by(Team.team_id).tuples()]
    rows = {r.team_id_id: r for r in Autopilot.select().where(Autopilot.team_id.in_(owned))} if owned else {}
    return [(t, rows.get(t)) for t in owned]


@db_operation("ai.autopilot_row")
def _autopilot_row(team_id: int) -> Optional[Autopilot]:
    return Autopilot.get_or_none(Autopilot.team_id == team_id)


async def _set_autopilot(team_id: int, user_id: int, on: bool, now: datetime) -> Autopilot:
    """Check-ins on (started, or resumed) or off (paused): the Check-ins row (services/ai/levels.py)."""
    return await levels.apply(team_id, user_id, levels.Change(checkins=on), now)


@db_operation("ai.owned_team_id")
def _owns(team_id: int, user_id: int) -> bool:
    return Team.select().where(Team.team_id == team_id, Team.user_id == user_id).exists()


# ------------------------------- the brief ------------------------------- #


def title_of(kind: str, nba_date: date) -> str:
    return f"{LABELS[kind]} · {nba_date.strftime('%a %b')} {nba_date.day}"


_SHADOW = (
    "You run it in shadow: nothing you do reaches ESPN. propose_lineup and propose_move check a change and "
    "keep it as what you would have done (the user can't send it); log_decision records each call you make, "
    "holds included. The user isn't here while you work: they read this conversation later and mark each "
    "call agree or disagree."
)

# A change to an automation's plan is a chip too (Dime's steering, the plan's step 4): steer it with
# holds, or change the lane or the pickup, and cite that
_REVIEW_STEER = (
    "- Judge each automation's plan: auto-lineup's fill, each lane's next decision, each scheduled pickup, "
    "and your own holds (get_automations lists them). Log one decision for each, a hold when you'd keep it. "
    "For a lineup or roster change, propose it first and cite the proposal. To change what an automation "
    "does, steer it rather than work around it: steer holds (keep, block, override, pin, or clear one "
    "that no longer fits), set_lane, or cancel_pickup, and cite that chip.\n"
)
_REVIEW = (
    _REVIEW_STEER +
    "- Then look for what no automation does: an add or drop worth making (run your playbook's checks "
    "first, and project_matchup it). Log it as a change with automation none, or log that you'd stand pat.\n"
    "- remember what the follow-up should check (a thesis, a player to watch), with a recheck day."
)

_STEPS = {
    MORNING: (
        "Go through the day:\n"
        "- recall your plans for the team, then get_automations for what the automations will do today "
        "and next.\n"
        "- get_lineup for today, get_player_status for anyone doubtful, project_matchup for where the "
        "matchup stands.\n" + _REVIEW
    ),
    WEEKLY: (
        "It's the matchup's first day, so this is the week's check-in:\n"
        "- recall your plans, then get_decisions and get_move_outcomes for last week: which of your calls "
        "were right, and which moves paid.\n"
        "- get_matchup and get_league for this week's opponent and the race; set the week's plan (where to "
        "stream, which categories or how many adds to spend) and remember it as a plan, revising last week's.\n"
        "- get_automations, get_lineup and project_matchup for today.\n" + _REVIEW
    ),
    PRE_LOCK: (
        "The first game tips soon:\n"
        "- get_automations for what auto-lineup wrote today and any late-scratch swap, then get_lineup for "
        "today and get_player_status for anyone doubtful.\n"
        "- Log one lineup decision: hold if the lineup is right, or propose_lineup the change and cite it.\n"
        "- Log another call only if something changed since the morning (a scratch, a lane's pickup that "
        "no longer fits)."
    ),
    NIGHT: (
        "Follow up on that day's games:\n"
        "- get_decisions since that day for what your check-ins decided, then get_game_log for the players "
        "the calls were about and get_move_outcomes for the moves.\n"
        "- For each call, say whether it was right and why, against what happened. A disagree from the "
        "user is a call to learn from too: read why if they said.\n"
        "- remember a lesson only when several cases point the same way, naming them; revise a note the "
        "night proved wrong.\n"
        "- Log a decision only for something that needs a call now."
    ),
    EVENT: (
        "Something came in about players on the team (listed above). Decide whether anything should change now:\n"
        "- get_player_status for each player named (his verdict for his next game, its source, the latest news), "
        "get_player_news when the news needs more, and get_automations for what auto-lineup and the "
        "late-scratch check have written or will write today.\n"
        "- get_lineup for the day his game is on. If he is out and in a starting slot, or cleared and on the bench "
        "while a worse player starts, propose_lineup the change, unless the fill or the late-scratch check "
        "already makes it.\n"
        "- If he will miss more than that game (a long absence, a lost role), weigh an add or a drop: run your "
        "playbook's checks and project_matchup it first, or hold and say why.\n" +
        "- To keep an automation from undoing your call, steer it: a pin, or an override at zero for the days "
        "he is out, ending when he is due back.\n"
        "- Log one decision for each player named: a change citing its proposal, or a hold and why.\n"
        "- remember what the follow-up should check, with a recheck day."
    ),
}


def acting_rules(rows: list[str], *, event: bool = False) -> str:
    """The rules of a check-in on a team where Dime acts on some automations (the plan's step 5). An
    event check-in's moves all count under late scratches (autonomy.rows_of), so its rules say that."""
    what = ("Late scratches are at AI on this team and this is their check-in: every move you make in it counts "
            "under them, whatever it changes (slots, adds and drops, lanes). "
            if event else
            f"What you do yourself on this team: {autonomy.words(rows)}. Those automations are at AI; on the "
            "others you review, as in shadow. ")
    return what + (
        "Every proposal you make in a check-in is first logged as what you would do; "
        "log_decision with call change and its proposal_id is what commits it: on an automation at AI it is sent "
        "now through the user's own write routes if the policy allows (get_automations' autopilot section has "
        "the caps, what you have used and what you did lately); anywhere else it stays a review, never sent. A "
        "move the policy blocks or ESPN refuses is left as a chip for the user; don't try it again in this "
        "check-in. So propose freely, but log as a change only what you'd do, and never say a move was made "
        "unless log_decision's action says sent. log_decision records each call you make, holds included. The "
        "user isn't here while you work: they read this conversation later and mark each call agree or disagree."
    )


def brief(kind: str, nba_date: date, team_id: int, *, manual: bool,
          acting: Optional[list[str]] = None, events: Sequence[str] = ()) -> str:
    """The check-in's opening message: what it is, its rules (shadow, or which automations Dime acts
    on), what came in since the last check-in (`events`, one line each), and the steps for its kind.
    Acting on none and no events, it is step 3's brief exactly."""
    who = ("The user asked for it from the AI desk." if manual
           else "It started because news came in about the roster." if kind == EVENT
           else "It started on its schedule.")
    # What an event check-in commits counts under late scratches alone (autonomy.rows_of), and a
    # scheduled one's under each move's own row: late scratches act only in event check-ins
    if kind == EVENT:
        acting = [autonomy.LATE_SCRATCH] if autonomy.LATE_SCRATCH in (acting or []) else []
    else:
        acting = [r for r in acting or [] if r != autonomy.LATE_SCRATCH]
    live = bool(acting)
    rules = acting_rules(acting, event=kind == EVENT) if live else _SHADOW
    head = (f"[{LABELS[kind]} for team_id {team_id}, about {nba_date.strftime('%A')} {nba_date.isoformat()}. "
            f"{who} {rules}]")
    close = ("End with a short note for the user: what you sent, what you left for them to send, and what you'd "
             "keep, and why, in a few lines. Your decisions show as their own blocks, so don't list them again."
             if live else
             "End with a short note for the user: what you'd change and what you'd keep, and why, in a few "
             "lines. Your decisions show as their own blocks, so don't list them again.")
    news = ""
    if events:
        lead = ("What came in:" if kind == EVENT else
                "What came in since your last check-in (weigh it in your calls, and log a call on any that "
                "changes one):")
        news = lead + "\n" + "\n".join(events) + "\n\n"
    return f"{head}\n\n{news}{_STEPS[kind]}\n\n{close}"


# ------------------------------- running one ------------------------------- #

# Check-ins still running, held so they aren't collected
_RUNNING: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Started:
    checkin_id: int
    team_id: int
    kind: str
    nba_date: date


async def _start(team_id: int, user_id: int, kind: str, nba_date: date, due_at: datetime, *, manual: bool,
                 now: datetime) -> Optional[int]:
    """Claim the check-in, reserve its spend, open its conversation and start its turn. Returns its id,
    or None when another tick has it. A scheduled one the budget refuses is claimed as `skipped`; a
    manual one raises instead."""
    reservation_id = await spend.reserve_autopilot(user_id)
    if reservation_id is None:
        if manual:
            raise ServiceUnavailableError("AI_AUTOPILOT_BUDGET_REACHED",
                                          "Dime's check-ins have used this month's budget; it resets on the 1st",
                                          log_level="warning")
        await _claim(team_id, user_id, kind, nba_date, due_at, manual=False, now=now, state=SKIPPED, reason="budget")
        return None
    checkin_id = await _claim(team_id, user_id, kind, nba_date, due_at, manual=manual, now=now)
    if checkin_id is None:
        await spend.cancel(reservation_id)
        return None
    from services.ai import events
    autopilot: Optional[Autopilot] = None
    try:
        autopilot = await _autopilot_row(team_id)
        taken = await events.take(team_id, checkin_id, now, row=autopilot)
    except Exception:
        get_logger().exception("ai_checkin_events_failed", checkin_id=checkin_id, team_id=team_id)
        taken = []
    if kind == EVENT and not taken:
        # Another check-in took them between the kick and this claim
        await spend.cancel(reservation_id)
        await _settle(checkin_id, SKIPPED, "no_events", None, 0, now)
        return None
    conversation_id: Optional[int] = None
    try:
        conversation = await conversations.create(user_id, team_id, kind=conversations.CHECKIN,
                                                  title=title_of(kind, nba_date))
        conversation_id = conversation.id
        hold = timedelta(seconds=settings.autopilot_checkin_timeout_seconds + 30)
        await conversations.claim(conversation_id, user_id, hold)
        await _attach(checkin_id, conversation_id)
        header_line = await agent.header(user_id, team_id, conversation_id=conversation_id)
        acting = autonomy.acting_rows(autopilot if autopilot is not None else await _autopilot_row(team_id))
        turn = agent.Turn(conversation_id=conversation_id, user_id=user_id,
                          message=brief(kind, nba_date, team_id, manual=manual, acting=acting,
                                        events=events.brief_lines(taken)),
                          history=conversations.History(), reservation_id=reservation_id,
                          tools=agent.CHECKIN_TOOLS, allowed=agent.CHECKIN_ALLOWED, checkin_id=checkin_id,
                          limits=agent.Limits.checkin())
    except Exception:
        get_logger().exception("ai_checkin_start_failed", checkin_id=checkin_id, team_id=team_id, kind=kind)
        await spend.cancel(reservation_id)
        if taken:
            try:
                await events.release(checkin_id)
            except Exception:
                get_logger().exception("ai_checkin_events_release_failed", checkin_id=checkin_id)
        if conversation_id is not None:
            await conversations.release(conversation_id)
        await _settle(checkin_id, FAILED, "start_failed", None, 0, _now())
        if manual:
            raise
        return checkin_id
    task = asyncio.create_task(_run(turn, header_line, checkin_id, team_id, kind))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return checkin_id


async def _run(turn: agent.Turn, header_line: str, checkin_id: int, team_id: int, kind: str) -> None:
    """The check-in's turn, then its row settled with the turn's outcome and cost, then the team kicked
    for any event that came in while it ran."""
    result: Optional[agent.TurnResult] = None
    try:
        result = await agent.run_turn(turn, agent.Emitter(), header_line=header_line)
    finally:
        ok = result is not None and result.outcome == "ok"
        state, reason = (DONE, None) if ok else (FAILED, result.outcome if result is not None else "crashed")
        cost = result.cost if result is not None else turn.cost
        try:
            await _settle(checkin_id, state, reason, result.turn_id if result is not None else None, cost, _now())
        except Exception:
            get_logger().exception("ai_checkin_settle_failed", checkin_id=checkin_id)
        get_logger().info("ai_checkin", checkin_id=checkin_id, team_id=team_id, kind=kind, state=state,
                          reason=reason, cost_micros=cost, decisions=turn.decisions, proposals=len(turn.proposals))
        await _after(team_id, turn.user_id)


async def _after(team_id: int, user_id: int) -> None:
    """Once a check-in is settled: what it did may have changed the roster, and events that came in
    while it ran wait for a kick."""
    from services.ai import events
    if not events.enabled():
        return
    # A move it made (or one the user sent from its chips meanwhile) may have changed the roster
    events.forget_roster(team_id)
    try:
        await events.kick(team_id, user_id, _now())
    except Exception:
        get_logger().exception("ai_checkin_event_kick_failed", team_id=team_id)


async def start_slot(row: Autopilot, slot: Slot, now: datetime) -> Optional[Started]:
    """Start a scheduled check-in that is due (an event kick found it so): it takes the team's
    pending events. Within the day's cap, like the tick (desk runs don't count)."""
    team_id, user_id = row.team_id_id, row.user_id_id
    if await _ran_since(team_id, now - timedelta(days=1), scheduled_only=True) >= settings.autopilot_checkins_per_day:
        return None
    checkin_id = await _start(team_id, user_id, slot.kind, slot.nba_date, slot.due_at, manual=False, now=now)
    return None if checkin_id is None else Started(checkin_id, team_id, slot.kind, slot.nba_date)


async def start_event(row: Autopilot, today: date, now: datetime) -> Optional[Started]:
    """Start an event check-in about `today` for the team's pending events (events.kick checked the caps)."""
    team_id, user_id = row.team_id_id, row.user_id_id
    checkin_id = await _start(team_id, user_id, EVENT, today, now, manual=False, now=now)
    return None if checkin_id is None else Started(checkin_id, team_id, EVENT, today)


async def _tick_team(row: Autopilot, slots: list[Slot], days: list[date], now: datetime,
                     out: dict[str, Any]) -> None:
    """One team's share of a tick: the first due check-in started, closed windows recorded as missed,
    and, with nothing running, the team's waiting events given their turn (events.kick)."""
    team_id, user_id = row.team_id_id, row.user_id_id
    taken = await _taken(team_id, days)
    busy = await _busy(team_id)
    # The day's cap counts scheduled check-ins only: runs from the desk never use up a slot
    ran = await _ran_since(team_id, now - timedelta(days=1), scheduled_only=True)
    for slot in slots:
        if (slot.kind, slot.nba_date) in taken:
            continue
        state = standing(slot, now, row.active_since)
        if state == "missed":
            if await _claim(team_id, user_id, slot.kind, slot.nba_date, slot.due_at, manual=False, now=now,
                            state=MISSED, reason="window_closed") is not None:
                out["missed"] += 1
        elif state == "due" and not busy:
            if ran >= settings.autopilot_checkins_per_day:
                if await _claim(team_id, user_id, slot.kind, slot.nba_date, slot.due_at, manual=False, now=now,
                                state=SKIPPED, reason="daily_cap") is not None:
                    out["skipped"] += 1
                continue
            checkin_id = await _start(team_id, user_id, slot.kind, slot.nba_date, slot.due_at, manual=False,
                                      now=now)
            if checkin_id is None:
                out["skipped"] += 1
                continue
            busy, ran = True, ran + 1
            out["started"].append(Started(checkin_id, team_id, slot.kind, slot.nba_date))
    if not busy:
        # Events that waited (a check-in was running, one was about to open): their turn now
        started = await _kick_events(row, now)
        if started is not None:
            out["started"].append(started)


async def run_due(now: Optional[datetime] = None) -> dict[str, Any]:
    """One tick of the schedule: close check-ins that died with their process, then for each
    switched-on team start the first check-in that is due (one at a time per team, within the
    day's cap), and record the ones whose window closed as missed."""
    now = now or _now()
    out: dict[str, Any] = {"off": False, "teams": 0, "started": [], "missed": 0, "skipped": 0, "stale": 0}
    if not settings.ai_enabled or not settings.autopilot_enabled:
        out["off"] = True
        return out
    out["stale"] = await _sweep(now - timedelta(seconds=settings.autopilot_checkin_timeout_seconds) - _STALE_MARGIN,
                                now)
    rows = [r for r in await _live() if allowed(r.team_id_id, r.user_id_id)]
    out["teams"] = len(rows)
    if not rows:
        return out
    cal = await calendar(now)
    # Yesterday's too: a check-in whose window closed while no tick ran (the scheduler down past
    # the 3 AM rollover) is still recorded as missed. Only today's can be due.
    slots = cal.slots(cal.today - timedelta(days=1)) + cal.slots()
    days = sorted({s.nba_date for s in slots})
    for row in rows:
        try:
            await _tick_team(row, slots, days, now, out)
        except Exception:
            # One team's failure (its reads, a claim, a start) doesn't hold back the others this tick
            get_logger().exception("ai_checkins_team_failed", team_id=row.team_id_id)
    get_logger().info("ai_checkins_tick", teams=out["teams"], started=len(out["started"]), missed=out["missed"],
                      skipped=out["skipped"], stale=out["stale"])
    return out


async def _kick_events(row: Autopilot, now: datetime) -> Optional[Started]:
    from services.ai import events
    if not events.enabled():
        return None
    try:
        return await events.kick(row.team_id_id, row.user_id_id, now, row=row)
    except Exception:
        get_logger().exception("ai_checkin_event_kick_failed", team_id=row.team_id_id)
        return None


# ------------------------------- the AI desk ------------------------------- #


def _ensure_allowed(team_id: int, user_id: int) -> None:
    if not settings.autopilot_enabled:
        raise ServiceUnavailableError("AI_AUTOPILOT_DISABLED", "Dime's check-ins are turned off", log_level="info")
    if not allowed(team_id, user_id):
        raise AuthorizationError("AI_AUTOPILOT_NOT_ALLOWED", "Dime's check-ins aren't open for this team yet")


async def _ensure_owned(team_id: int, user_id: int) -> None:
    if not await _owns(team_id, user_id):
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")


async def run_now(user_id: int, team_id: int, kind: Optional[str] = None,
                  now: Optional[datetime] = None) -> AiCheckin:
    """A check-in the user starts from the AI desk, on or off the schedule (it doesn't take a slot).
    Without `kind`, the clock picks: morning (weekly on a matchup's first day), pre-lock in the hours
    before the first tip, the night follow-up on yesterday after it."""
    now = now or _now()
    await _ensure_owned(team_id, user_id)
    _ensure_allowed(team_id, user_id)
    if kind is not None and kind not in KINDS:
        raise AppError("AI_CHECKIN_INVALID", "A check-in is morning, weekly, pre_lock or night", status_code=422)
    if await _busy(team_id):
        raise ConflictError("AI_CHECKIN_RUNNING", "A check-in is already running on this team; wait for it to finish")
    if await _ran_since(team_id, now - timedelta(days=1)) >= settings.autopilot_checkins_per_day:
        raise AppError("AI_CHECKIN_DAILY_LIMIT", "This team has had today's check-ins; try again tomorrow",
                       status_code=429, api_status=ApiStatus.RATE_LIMITED, log_level="info")
    cal = await calendar(now)
    kind = kind or default_kind(now, cal.today, cal.tips.get(cal.today), cal.matchup_start)
    checkin_id = await _start(team_id, user_id, kind, day_of(kind, cal.today), now, manual=True, now=now)
    if checkin_id is None:  # one started between the check above and the claim
        raise ConflictError("AI_CHECKIN_RUNNING", "A check-in is already running on this team; wait for it to finish")
    row = await _get(checkin_id)
    if row is None:
        raise AppError("AI_CHECKIN_INVALID", "The check-in didn't start; try again", status_code=500)
    return row


async def set_autopilot(user_id: int, team_id: int, change: levels.Change) -> Autopilot:
    """The team's automation levels and autopilot switch (services/ai/levels.py). Check-ins on starts
    (or resumes) them, off pauses them; a pause Dime made itself is lifted the same way."""
    await _ensure_owned(team_id, user_id)
    _ensure_allowed(team_id, user_id)
    prefs, _ = await levels.prefs_of(user_id, [])
    checked = levels.check(change, email=levels.lineup_email(prefs))
    return await levels.apply(team_id, user_id, checked, _now())


def next_slot(cal: Calendar, row: Optional[Autopilot], taken: set[tuple[str, date]],
              now: datetime) -> Optional[Slot]:
    """The team's next scheduled check-in: today's first that is still open and not yet run, or
    tomorrow's first."""
    if row is None or row.paused_at is not None:
        return None
    for slot in cal.slots():
        if (slot.kind, slot.nba_date) not in taken and now < slot.closes_at and slot.closes_at > row.active_since:
            return slot
    tomorrow = cal.slots(cal.today + timedelta(days=1))
    return tomorrow[0] if tomorrow else None


async def teams_of(user_id: int, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """The user's teams Dime may run check-ins on, with each one's switch and next check-in."""
    now = now or _now()
    pairs = await _autopilot_teams(user_id, [t for t in settings.autopilot_team_ids
                                             if user_id in settings.ai_agent_user_ids])
    if not pairs:
        return []
    cal = await calendar(now)
    days = sorted({s.nba_date for s in cal.slots()})
    prefs, team_prefs = await levels.prefs_of(user_id, [t for t, _ in pairs])
    out = []
    for team_id, row in pairs:
        taken = await _taken(team_id, days) if row is not None else set()
        out.append(team_view(team_id, row, next_slot(cal, row, taken, now), prefs, team_prefs.get(team_id)))
    return out


def team_view(team_id: int, row: Optional[Autopilot], upcoming: Optional[Slot], prefs: Any = None,
              team_pref: Any = None) -> dict[str, Any]:
    """The team's switch, its next check-in and its automations table (`prefs` and `team_pref` are the
    account's notification preferences and the team's override, which hold lineup's and late scratches'
    levels below AI)."""
    return {
        "team_id": team_id,
        "on": row is not None and row.paused_at is None,
        "active_since": row.active_since if row is not None else None,
        "paused_at": row.paused_at if row is not None else None,
        "paused_by": row.paused_by if row is not None else None,
        "paused_reason": row.paused_reason if row is not None else None,
        "next": None if upcoming is None else {"kind": upcoming.kind, "date": upcoming.nba_date,
                                               "due_at": upcoming.due_at},
        "autopilot": autonomy.switch_on(row),
        "rows": autonomy.table(row, lineup_below_ai=levels.lineup_below_ai(prefs, team_pref),
                               lineup_email=levels.lineup_email(prefs),
                               late_scratch_below_ai=levels.late_scratch_below_ai(prefs, team_pref)),
    }


def summary_of(row: AiCheckin, tally: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    t = tally or {}
    return {
        "checkin_id": row.id,
        "team_id": row.team_id_id,
        "kind": row.kind,
        "date": row.nba_date,
        "manual": row.manual,
        "state": row.state,
        "reason": row.reason,
        "due_at": row.due_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "conversation_id": row.conversation_id_id,
        "title": title_of(row.kind, row.nba_date),
        "cost_usd": (row.cost_micros or 0) / 1e6,
        "decisions": t.get("decisions", 0),
        "changes": t.get("changes", 0),
        "agree": t.get("agree", 0),
        "disagree": t.get("disagree", 0),
        "sent": t.get("sent", 0),
        "events": t.get("events", []),
    }


async def inbox(user_id: int, team_id: Optional[int] = None, limit: int = 30) -> list[dict[str, Any]]:
    rows, tallies = await _list(user_id, team_id, limit)
    return [summary_of(r, tallies.get(r.id)) for r in rows]


async def summary(checkin_id: int) -> Optional[dict[str, Any]]:
    row = await _get(checkin_id)
    return None if row is None else summary_of(row, await _tally(checkin_id))


async def of_conversation(conversation_id: int, user_id: int) -> Optional[dict[str, Any]]:
    row = await _of_conversation(conversation_id, user_id)
    return None if row is None else summary_of(row, await _tally(row.id))
