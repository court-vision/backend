"""
Dime acting on its own (the Dime autopilot plan, step 5, "Its own moves").

A check-in decides with `log_decision`. When a decision is a change and cites the
proposal that checked it, that decision is the commitment, and the team's automation
levels (services/ai/autonomy.py) say what happens next:

    review   the move's rows aren't all at AI (or check-ins are paused, or acting is
             off on the server): the proposal stays what Dime would have done (step 3)
    ai       every row the move counts under is at AI: the proposal becomes a chip,
             is held to the policy, and is sent by the server

Sending goes through exactly the routes the user's own sends take, with the chip's
`proposal_id`, so every check those routes make still runs and the chip is marked
sent: a roster transaction (`RosterTransactionService.apply`), a scheduled pickup
(`ScheduledPickupService.schedule`), a lineup write (`LineupEditorService.
apply_manual`), the holds route (`apply_holds`), the lane routes and a pickup's
cancel. An ESPN write is audited in usr.roster_moves as source `autopilot`, which
the auto-lineup once-a-day index never counts; a pickup Dime schedules is marked
`made_by_autopilot` so the executor's write for it says so too.

The policy (`blocked_by`), checked just before the send, every rule a line in the
plan's Guardrails table:

    switches       AI_ENABLED, AUTOPILOT_ENABLED, the team's Pause and allowlist,
                   ROSTER_WRITES_ENABLED for an ESPN write, DIME_STEERING_ENABLED for
                   a steering chip
    caps           Dime's own adds: AUTOPILOT_ADDS_PER_DAY on the move's day and
                   AUTOPILOT_ADDS_PER_MATCHUP over the matchup (lanes keep their own
                   budget, and ESPN's limits still apply at the write)
    reserve        AUTOPILOT_ADDS_RESERVE of ESPN's adds this matchup are left for the
                   user (injuries)
    protected      never drops one of the roster's top AUTOPILOT_PROTECTED_TOP by the
                   board's value (Dime's own keep holds already refuse at the proposal)
    churn          never drops a player added in the last AUTOPILOT_CHURN_HOURS unless
                   he is out

Every committed change on rows at AI is a row in usr.autopilot_actions:
sent, blocked (with the rule), refused (the route or ESPN said no) or failed. One
per proposal, so a second decision citing the same move never sends it twice. A
move that is blocked or refused stays a chip the user can still send.

Commits run one at a time per team (`_lock`), so the caps count every send before them.

Auto-pause (`_after`): the team pauses itself (paused_by `system`, the Pause the
user lifts) and emails the user after AUTOPILOT_PAUSE_REFUSED refused writes in a
day, AUTOPILOT_PAUSE_SAME_BLOCK blocks by the same rule in a day, or any ESPN
credential error. A switch that is off is not Dime's doing and never counts.

Nothing here runs in the AI desk's chat: there the user is present and confirms
every chip. Only a check-in's own `log_decision` commits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from peewee import IntegrityError, fn

from core.errors import AppError, ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import db_operation, run_db
from db.models.ai_conversations import AiProposal
from db.models.autopilot import AiCheckin, Autopilot, AutopilotAction
from services import schedule_service, steering
from services.ai import autonomy, proposals
from services.scheduled_pickup_service import rollover_at

log = get_logger("ai.acting")

SENT, BLOCKED, REFUSED, FAILED = "sent", "blocked", "refused", "failed"
OUTCOMES = (SENT, BLOCKED, REFUSED, FAILED)
AUTOPILOT = "autopilot"          # usr.roster_moves.source of every ESPN write Dime makes itself

# The policy's rules, as `rule` names them
R_AI_OFF, R_AUTOPILOT_OFF, R_PAUSED, R_NOT_ALLOWED = "ai_off", "autopilot_off", "paused", "not_allowed"
R_WRITES_OFF, R_STEERING_OFF, R_EXPIRED = "writes_off", "steering_off", "expired"
R_CAP_DAY, R_CAP_MATCHUP, R_RESERVE, R_PROTECTED, R_CHURN = "cap_day", "cap_matchup", "reserve", "protected", "churn"
R_CREDENTIALS = "credentials"
# Switches: off, they stop Dime, but trying wasn't Dime's mistake, so they never count toward auto-pause
SWITCHES = frozenset({R_AI_OFF, R_AUTOPILOT_OFF, R_PAUSED, R_NOT_ALLOWED, R_WRITES_OFF, R_STEERING_OFF, R_EXPIRED})
# The chips whose send is an ESPN write (a lane start writes nothing at once, but its pickups will)
_WRITES = frozenset({proposals.TRANSACTION, proposals.SCHEDULED, proposals.LINEUP})
MESSAGE_MAX = 400


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Verdict:
    """What became of a committed change: `review` (logged, never sent) or `ai` (Dime handled it)
    and, when Dime acted, the outcome, the rule or error behind it, and the words the user and the
    model read."""

    level: str
    outcome: Optional[str] = None
    rule: Optional[str] = None
    message: str = ""
    audit_id: Optional[int] = None
    action_id: Optional[int] = None
    paused: Optional[str] = None     # the team paused itself on this one: why

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"level": self.level, "outcome": self.outcome, "message": self.message}
        if self.rule:
            out["rule"] = self.rule
        if self.paused:
            out["paused"] = self.paused
        return out


# ------------------------------- DB ------------------------------- #


@db_operation("acting.autopilot_row")
def _autopilot_row(team_id: int) -> Optional[Autopilot]:
    return Autopilot.get_or_none(Autopilot.team_id == team_id)


@db_operation("acting.proposal")
def _proposal(proposal_id: int) -> Optional[AiProposal]:
    return AiProposal.get_or_none(AiProposal.id == proposal_id)


@db_operation("acting.promote")
def _promote(proposal_id: int, summary: str, now: datetime) -> bool:
    """A shadow proposal made a live chip: one conditional write, so only a shadow row moves."""
    return (AiProposal.update(state=proposals.PROPOSED, summary=summary, updated_at=now)
            .where((AiProposal.id == proposal_id) & (AiProposal.state == proposals.SHADOW)
                   & (AiProposal.expires_at > now))
            .execute()) > 0


@db_operation("acting.action_of")
def _action_of(proposal_id: int) -> Optional[AutopilotAction]:
    return AutopilotAction.get_or_none(AutopilotAction.proposal_id == proposal_id)


@db_operation("acting.record")
def _record(**fields: Any) -> Optional[AutopilotAction]:
    try:
        return AutopilotAction.create(**fields)
    except IntegrityError:          # another decision recorded this proposal first
        return None


@db_operation("acting.adds_sent")
def _adds_sent(team_id: int, start: date, end: date) -> int:
    """Dime's own adds that went out for days start..end."""
    total = (AutopilotAction.select(fn.COALESCE(fn.SUM(AutopilotAction.adds), 0))
             .where((AutopilotAction.team_id == team_id) & (AutopilotAction.outcome == SENT)
                    & (AutopilotAction.nba_date.between(start, end)))
             .scalar())
    return int(total or 0)


@db_operation("acting.count_since")
def _count_since(team_id: int, since: datetime, outcome: str, rule: Optional[str] = None) -> int:
    where = ((AutopilotAction.team_id == team_id) & (AutopilotAction.outcome == outcome)
             & (AutopilotAction.created_at >= since))
    if rule is not None:
        where &= AutopilotAction.rule == rule
    return AutopilotAction.select().where(where).count()


@db_operation("acting.recent_adds")
def _recent_adds(team_id: int, player_id: int, since: datetime, since_day: date) -> bool:
    """The player joined the team recently: a Court Vision add that went through since `since`
    (any source: the desk, a pickup, a lane, Dime), or a move the ledger read off the rosters
    (made in ESPN itself) counting from `since_day` on."""
    from db.models.move_outcomes import MoveOutcome
    from db.models.roster_moves import RosterMove
    rows = (RosterMove.select(RosterMove.moves)
            .where((RosterMove.team == team_id) & (RosterMove.kind == "transaction")
                   & RosterMove.status.in_(["applied", "applied_unverified"]) & (RosterMove.created_at >= since)))
    for r in rows:
        if any(m.get("action") == "add" and m.get("player_id") == player_id for m in (r.moves or [])):
            return True
    return (MoveOutcome.select()
            .where((MoveOutcome.team_id == team_id) & (MoveOutcome.add_player_id == player_id)
                   & (MoveOutcome.nba_date >= since_day))
            .exists())


async def _team(team_id: int, user_id: int) -> tuple[Any, Any]:
    """The team as the routes get it (owned, credentials hydrated), or (None, None)."""
    from api.deps import ensure_team_owned, load_owned_league_info
    team = await run_db("acting.team", ensure_team_owned, team_id, user_id)
    if team is None:
        return None, None
    return team, await load_owned_league_info(team)


@db_operation("acting.pause")
def _pause(team_id: int, reason: str, now: datetime) -> bool:
    """Pause the team on Dime's own account; False when it was already paused."""
    return (Autopilot.update(paused_at=now, paused_by="system", paused_reason=reason[:200], updated_at=now)
            .where((Autopilot.team_id == team_id) & Autopilot.paused_at.is_null(True))
            .execute()) > 0


@db_operation("acting.actions_of_decisions")
def _of_decisions(decision_ids: list[int]) -> dict[int, AutopilotAction]:
    if not decision_ids:
        return {}
    return {a.decision_id_id: a for a in AutopilotAction.select().where(AutopilotAction.decision_id.in_(decision_ids))}


@db_operation("acting.sent_of_checkins")
def _sent_of_checkins(checkin_ids: list[int]) -> dict[int, int]:
    if not checkin_ids:
        return {}
    rows = (AutopilotAction.select(AutopilotAction.checkin_id, fn.COUNT(AutopilotAction.id))
            .where(AutopilotAction.checkin_id.in_(checkin_ids) & (AutopilotAction.outcome == SENT))
            .group_by(AutopilotAction.checkin_id).tuples())
    return {cid: int(n) for cid, n in rows}


@db_operation("acting.recent")
def _recent(team_id: int, limit: int) -> list[AutopilotAction]:
    return list(AutopilotAction.select().where(AutopilotAction.team_id == team_id)
                .order_by(AutopilotAction.created_at.desc(), AutopilotAction.id.desc()).limit(limit))


# ------------------------------- shapes ------------------------------- #


def action_view(row: Any) -> dict[str, Any]:
    """An action as a decision block carries it (an `AiDecisionAction`)."""
    return {"outcome": row.outcome, "rule": row.rule, "message": row.message,
            "at": _aware(row.created_at).isoformat() if row.created_at else None}


async def actions_of(decision_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The action each decision led to, by decision id (the ones that led to none are absent)."""
    rows = await _of_decisions(decision_ids)
    return {did: action_view(a) for did, a in rows.items()}


async def sent_counts(checkin_ids: list[int]) -> dict[int, int]:
    """How many moves each check-in sent on its own."""
    return await _sent_of_checkins(checkin_ids)


def live_summary(row: Any) -> str:
    """The chip's own words once it is live: what its kind says outside a check-in."""
    action = row.action or {}
    if row.kind in (proposals.TRANSACTION, proposals.SCHEDULED):
        return proposals.summary_of(row.kind, row.add_name, row.drop_name, row.nba_date)
    if row.kind == proposals.LINEUP:
        return proposals.lineup_summary(row.moves or [], row.nba_date, row.nba_date == schedule_service.get_nba_today())
    from services.ai import steering as ai_steering   # it builds on proposals, as this does
    if row.kind == proposals.STEER:
        return ai_steering.steer_summary(action.get("holds") or [], action.get("clear") or [], False)
    if row.kind == proposals.LANE:
        holder = (action.get("holder") or {}).get("name") or "the spot's player"
        end = date.fromisoformat(action["end_date"]) if action.get("end_date") and action.get("op") == "start" else None
        return ai_steering.lane_summary(action.get("op") or "start", holder, end_date=end, gain=action.get("gain"),
                                        adds=action.get("adds"), shadow=False)
    if row.kind == proposals.CANCEL_PICKUP:
        drop = action.get("drop") or {}
        return ai_steering.pickup_summary((action.get("add") or {}).get("name") or "a player", drop.get("name"),
                                          row.nba_date, action.get("lane_id") is not None, False)
    return row.summary


# ------------------------------- the policy ------------------------------- #


def _top(board: Any, n: int) -> set[int]:
    """The roster's top `n` by the board's value (the Week desk's avg value), by ESPN id."""
    ranked = sorted(board.players, key=lambda p: p.avg_points or 0.0, reverse=True)
    return {p.player_id for p in ranked[:max(0, n)]}


async def blocked_by(team: Any, league_info: Any, row: Any, autopilot: Any, now: datetime
                     ) -> Optional[tuple[str, str]]:
    """The first rule the move breaks, as (rule, the sentence the user reads); None when it may go."""
    from services.ai import checkins
    team_id = team.team_id
    if not settings.ai_enabled:
        return R_AI_OFF, "The AI is switched off"
    if not settings.autopilot_enabled:
        return R_AUTOPILOT_OFF, "Autopilot is switched off on the server"
    if autopilot is None or autopilot.paused_at is not None:
        return R_PAUSED, "Autopilot is paused on this team"
    if not checkins.allowed(team_id, team.user_id):
        return R_NOT_ALLOWED, "This team isn't open to autopilot"
    if _aware(row.expires_at) <= now:
        return R_EXPIRED, "The move's day has started, so it can't be sent as proposed"
    if row.kind in _WRITES or (row.kind == proposals.LANE and (row.action or {}).get("op") == "start"):
        if not settings.roster_writes_enabled:
            return R_WRITES_OFF, "Roster writes are switched off on the server"
    if row.kind in proposals.STEERING_KINDS and not steering.enabled():
        return R_STEERING_OFF, "Dime's steering is switched off on the server"
    if row.kind not in (proposals.TRANSACTION, proposals.SCHEDULED):
        return None

    board = await proposals._board(team, league_info)
    if row.add_player_id is not None:
        day_used = await _adds_sent(team_id, row.nba_date, row.nba_date)
        if day_used >= settings.autopilot_adds_per_day:
            return R_CAP_DAY, (f"Dime has made its {settings.autopilot_adds_per_day} add(s) for "
                               f"{row.nba_date:%a %b} {row.nba_date.day}")
        week = schedule_service.get_current_matchup(schedule_service.get_nba_today())
        if week is not None and week["start_date"] <= row.nba_date <= week["end_date"]:
            used = await _adds_sent(team_id, week["start_date"], week["end_date"])
            if used >= settings.autopilot_adds_per_matchup:
                return R_CAP_MATCHUP, f"Dime has made its {settings.autopilot_adds_per_matchup} add(s) this matchup"
        reserve = reserve_block(board.acquisitions, row.nba_date)
        if reserve is not None:
            return R_RESERVE, reserve
    if row.drop_player_id is not None:
        held = next((p for p in board.players if p.player_id == row.drop_player_id), None)
        name = held.name if held else (row.drop_name or "That player")
        if row.drop_player_id in _top(board, settings.autopilot_protected_top):
            return R_PROTECTED, f"{name} is one of the roster's top {settings.autopilot_protected_top}, which Dime never drops"
        out = held is not None and (held.injury_status or "").upper() == "OUT"
        since = now - timedelta(hours=settings.autopilot_churn_hours)
        since_day = schedule_service.get_nba_today() - timedelta(days=max(1, settings.autopilot_churn_hours // 24))
        if not out and await _recent_adds(team_id, row.drop_player_id, since, since_day):
            return R_CHURN, (f"{name} joined the team in the last {settings.autopilot_churn_hours} hours, and isn't "
                             "out, so Dime won't drop him yet")
    return None


def reserve_block(acq: Any, day: date) -> Optional[str]:
    """Why an add for `day` would take one of the league's adds Dime keeps for the user, as the
    sentence the user reads; None when there is room (or no per-matchup limit, or `day` is in a
    later matchup)."""
    from services.acquisitions import in_current_matchup, matchup_room
    room = matchup_room(acq) if acq is not None else None
    if room is not None and in_current_matchup(acq, day) and room <= settings.autopilot_adds_reserve:
        return f"{room} of the league's adds left this matchup; Dime keeps {settings.autopilot_adds_reserve} for you"
    return None


# A pickup Dime scheduled itself, re-checked when it comes due: its reasons (usr.scheduled_pickups.reason)
P_OFF, P_ACTING_OFF, P_PAUSED = "dime_off", "dime_acting_off", "dime_paused"
P_NOT_ALLOWED, P_NOT_ACTING, P_RESERVE = "dime_not_allowed", "dime_not_acting", "dime_reserve"


async def pickup_block(team_id: int, user_id: int) -> Optional[tuple[str, str]]:
    """Whether Dime still sends adds and drops itself on the team, asked when a pickup it scheduled
    comes due (services/scheduled_pickup_service.py): the switches, the team's pause and the daily
    actions level as they are now, not as they were when it was scheduled. (reason, sentence) when the pickup may no
    longer go; None when it may. The add caps aren't asked again: the pickup counted toward them
    when it was scheduled."""
    from services.ai import checkins
    if not settings.ai_enabled or not settings.autopilot_enabled:
        return P_OFF, "Dime is switched off on the server"
    if not autonomy.acting_available():
        return P_ACTING_OFF, "Dime's acting is switched off on the server"
    autopilot = await _autopilot_row(team_id)
    if autopilot is None or autopilot.paused_at is not None:
        return P_PAUSED, "Dime is paused on this team"
    if not checkins.allowed(team_id, user_id):
        return P_NOT_ALLOWED, "This team isn't open to autopilot"
    if not autonomy.at_ai(autopilot, autonomy.DAILY_ACTIONS):
        return P_NOT_ACTING, "Daily actions aren't at AI on this team any more"
    return None


# ------------------------------- the send ------------------------------- #


async def _send(team: Any, league_info: Any, row: Any) -> tuple[str, Optional[int]]:
    """Send the chip through its route, as the user's tray or the Week desk would; returns the route's
    message and the usr.roster_moves row of an ESPN write. Raises what the route raises."""
    from schemas.lineup_editor import ApplyLineupMovesReq, LineupMoveReq
    from schemas.roster_transaction import RosterTransactionReq
    from schemas.scheduled_pickup import SchedulePickupReq
    from schemas.stream_lane import StreamLaneStartReq, StreamLaneUpdateReq
    from services.ai import steering as ai_steering
    from services.lineup_editor_service import LineupEditorService
    from services.roster_transaction_service import RosterTransactionService
    from services.scheduled_pickup_service import ScheduledPickupService
    from services.stream_lane_service import StreamLaneService

    action = row.action or {}
    if row.kind == proposals.TRANSACTION:
        board = await proposals._board(team, league_info)
        req = RosterTransactionReq(add_player_id=row.add_player_id, drop_player_id=row.drop_player_id,
                                   expected_scoring_period_id=board.scoring_period_id,
                                   roster_version=board.roster_version, proposal_id=row.id)
        resp = await RosterTransactionService.apply(team, league_info, req, source=AUTOPILOT)
        return resp.message, resp.data.audit_id if resp.data else None
    if row.kind == proposals.SCHEDULED:
        req = SchedulePickupReq(add_player_id=row.add_player_id, drop_player_id=row.drop_player_id,
                                scoring_period_id=row.scoring_period_id, proposal_id=row.id)
        resp = await ScheduledPickupService.schedule(team, league_info, req, by_autopilot=True)
        return resp.message, None
    if row.kind == proposals.LINEUP:
        board = await proposals._board(team, league_info)
        if board.scoring_period_id is not None and row.scoring_period_id > board.scoring_period_id:
            board = await proposals._board(team, league_info, scoring_period_id=row.scoring_period_id)
        moves = proposals.live_moves(board, row)
        if not moves:
            await proposals.mark_sent(row.id)
            # Nothing to write, but the chip still says who sits: the fill mustn't start them again
            await steering.pin_benched(row.id, by="dime")
            return "The lineup was already set this way on ESPN", None
        req = ApplyLineupMovesReq(
            moves=[LineupMoveReq(player_id=m.player_id, from_slot_id=m.from_slot_id, to_slot_id=m.to_slot_id)
                   for m in moves],
            expected_scoring_period_id=board.scoring_period_id, roster_version=board.roster_version,
            proposal_id=row.id)
        resp = await LineupEditorService.apply_manual(team, league_info, req, source=AUTOPILOT)
        return resp.message, resp.data.audit_id if resp.data else None
    if row.kind == proposals.STEER:
        made = await ai_steering.apply_holds(team, row.id, by="dime")
        cleared = len(action.get("clear") or [])
        return f"Set {made} hold(s)" + (f", cleared {cleared}" if cleared else ""), None
    if row.kind == proposals.LANE:
        op = action.get("op")
        if op == "start":
            holder = (action.get("holder") or {}).get("espn_id")
            resp = await StreamLaneService.start(team, league_info,
                                                 StreamLaneStartReq(player_id=holder, proposal_id=row.id), by="dime")
        elif op in ("pause", "resume"):
            resp = await StreamLaneService.update(
                team, league_info, action.get("lane_id"),
                StreamLaneUpdateReq(status="paused" if op == "pause" else "active", proposal_id=row.id), by="dime")
        else:
            resp = await StreamLaneService.stop(team, action.get("lane_id"), row.id, by="dime")
        return resp.message, None
    if row.kind == proposals.CANCEL_PICKUP:
        resp = await ScheduledPickupService.cancel(team, action.get("pickup_id"), row.id)
        return resp.message, None
    raise AppError("AI_PROPOSAL_INVALID", f"Dime can't send a {row.kind} chip itself")


def _classify(exc: BaseException) -> tuple[str, str, str]:
    """(outcome, rule, message) for what a route raised."""
    from services.lineup_editor_service import RosterWriteDisabled, RosterWriteRejected
    if isinstance(exc, ProviderAuthError):
        return FAILED, R_CREDENTIALS, "ESPN refused the team's credentials"
    if isinstance(exc, RosterWriteDisabled):
        return BLOCKED, R_WRITES_OFF, "Roster writes are switched off on the server"
    if isinstance(exc, RosterWriteRejected):
        return REFUSED, "espn_rejected", f"ESPN refused it: {exc.message}"
    if isinstance(exc, AppError):
        code = exc.error_code or "error"
        if code == "AI_STEERING_OFF":
            return BLOCKED, R_STEERING_OFF, exc.message
        if 400 <= (exc.status_code or 500) < 500:
            return REFUSED, code, exc.message
        return FAILED, code, exc.message
    return FAILED, "error", "Something went wrong sending it"


# ------------------------------- auto-pause ------------------------------- #


async def _after(team: Any, league_info: Any, verdict: Verdict, now: datetime) -> Optional[str]:
    """Pause the team when this outcome is the one that should stop it; the reason when it did."""
    since = rollover_at(schedule_service.get_nba_today())
    reason: Optional[str] = None
    if verdict.outcome == FAILED and verdict.rule == R_CREDENTIALS:
        reason = "ESPN refused the team's credentials"
    elif verdict.outcome == REFUSED:
        n = await _count_since(team.team_id, since, REFUSED)
        if n >= settings.autopilot_pause_refused:
            reason = f"{n} of its moves were refused today"
    elif verdict.outcome == BLOCKED and verdict.rule not in SWITCHES:
        n = await _count_since(team.team_id, since, BLOCKED, verdict.rule)
        if n >= settings.autopilot_pause_same_block:
            reason = f"the policy stopped the same kind of move {n} times today ({verdict.rule})"
    if reason is None:
        return None
    if not await _pause(team.team_id, reason, now):
        return None
    log.warning("autopilot_auto_paused", team_id=team.team_id, reason=reason, rule=verdict.rule)
    try:
        await _email_paused(team, league_info, reason, verdict)
    except Exception:
        log.exception("autopilot_pause_email_failed", team_id=team.team_id)
    return reason


@db_operation("acting.recipient")
def _recipient(user_id: int) -> Optional[str]:
    from db.models.notifications import NotificationPreference
    from db.models.users import User
    user = User.get_or_none(User.user_id == user_id)
    if user is None:
        return None
    prefs = NotificationPreference.get_or_none(NotificationPreference.user == user_id)
    return (prefs.email if prefs is not None and prefs.email else None) or user.email


def pause_email(team_name: str, reason: str, last: str) -> tuple[str, str]:
    subject = f"Dime paused itself on {team_name}"
    body = "\n".join([
        f"Dime stopped acting on {team_name}: {reason}.",
        "",
        f"The last move it tried: {last}",
        "",
        "Nothing more goes out on its own until you resume it, and its check-ins wait too. Your own moves,",
        "auto-lineup and your stream lanes are untouched. Resume it in Account, AI and automation:",
        f"{settings.frontend_url.rstrip('/')}/account?v=automation",
        "",
        "-- Court Vision",
    ])
    return subject, body


async def _email_paused(team: Any, league_info: Any, reason: str, verdict: Verdict) -> None:
    from services.notification_service import NotificationService
    to = await _recipient(team.user_id)
    if not to:
        return
    subject, body = pause_email(getattr(league_info, "team_name", None) or "your team", reason, verdict.message)
    await asyncio.to_thread(NotificationService()._send_email, to, subject, body)


# ------------------------------- the commitment ------------------------------- #


# A check-in's tool calls run together (agent._run_tools), and two decisions in one response
# could each pass the caps before either is recorded: one commit at a time per team. One
# check-in runs per team (the database says so), in this process.
_locks: dict[int, asyncio.Lock] = {}


def _lock(team_id: int) -> asyncio.Lock:
    return _locks.setdefault(team_id, asyncio.Lock())


async def commit(user_id: int, checkin: AiCheckin, decision_id: int, proposal_id: int,
                 now: Optional[datetime] = None) -> Verdict:
    """What a check-in's change does under the team's levels: a review (nothing sent) unless every
    row the move counts under acts, and then the chip held to the policy and sent. Never raises for
    the move itself: whatever happens is the verdict, which the decision carries."""
    async with _lock(checkin.team_id_id):
        return await _commit(user_id, checkin, decision_id, proposal_id, now or _now())


async def _commit(user_id: int, checkin: AiCheckin, decision_id: int, proposal_id: int, now: datetime) -> Verdict:
    team_id = checkin.team_id_id
    row = await _proposal(proposal_id)
    if row is None or proposals.team_of(row) != team_id:
        return Verdict(autonomy.REVIEW)
    autopilot = await _autopilot_row(team_id)
    event = autonomy.is_event(getattr(checkin, "kind", None))
    if not autonomy.chip_acts(autopilot, row, event=event):
        return Verdict(autonomy.REVIEW)
    ai = autonomy.AI
    if row.state == proposals.SHADOW:
        if not await _promote(row.id, live_summary(row), now):
            return Verdict(autonomy.REVIEW, message="The move's day has started, so it stays logged")
        row = await _proposal(proposal_id)

    earlier = await _action_of(row.id)
    if earlier is not None:
        return Verdict(ai, earlier.outcome, earlier.rule, f"Already handled: {earlier.message}",
                       earlier.audit_id, earlier.id)
    if row.state != proposals.PROPOSED:
        return Verdict(ai, message=f"The chip is {row.state}, so Dime left it")

    team, league_info = await _team(team_id, user_id)
    if team is None:
        return Verdict(autonomy.REVIEW, message="The team is no longer the user's")
    base = dict(team_id=team_id, user_id=user_id, checkin_id=checkin.id, decision_id=decision_id, proposal_id=row.id,
                capability=autonomy.primary(autonomy.rows_of(row, event=event)), kind=row.kind,
                nba_date=row.nba_date, adds=autonomy.adds_of(row), created_at=now)

    verdict: Optional[Verdict] = None
    try:
        block = await blocked_by(team, league_info, row, autopilot, now)
    except Exception as exc:          # a read the policy needs failed: nothing was sent
        outcome, rule, message = _classify(exc)
        verdict = Verdict(ai, outcome, rule, f"{message}; left as a chip for the user")
    else:
        if block is not None:
            verdict = Verdict(ai, BLOCKED, block[0], f"{block[1]}; left as a chip for the user")
    if verdict is None:
        try:
            message, audit_id = await _send(team, league_info, row)
            verdict = Verdict(ai, SENT, None, f"Sent by Dime: {message}", audit_id)
        except Exception as exc:
            outcome, rule, message = _classify(exc)
            if outcome == FAILED and rule == "error":
                log.exception("autopilot_send_failed", team_id=team_id, proposal_id=row.id)
            verdict = Verdict(ai, outcome, rule, f"{message}; left as a chip for the user")

    recorded = await _record(**base, outcome=verdict.outcome, rule=verdict.rule,
                             message=verdict.message[:MESSAGE_MAX], audit_id=verdict.audit_id)
    log.info("autopilot_action", team_id=team_id, proposal_id=row.id, kind=row.kind, outcome=verdict.outcome,
             rule=verdict.rule, audit_id=verdict.audit_id, checkin_id=checkin.id)
    paused = await _after(team, league_info, verdict, now) if verdict.outcome != SENT else None
    return Verdict(ai, verdict.outcome, verdict.rule, verdict.message, verdict.audit_id,
                   recorded.id if recorded else None, paused)


# ------------------------------- what Dime reads ------------------------------- #


async def policy_view(team_id: int, autopilot: Any) -> dict[str, Any]:
    """The rows Dime acts on, the policy and what Dime has used of it, for get_automations (DB reads only)."""
    today = schedule_service.get_nba_today()
    week = schedule_service.get_current_matchup(today)
    used_today = await _adds_sent(team_id, today, today)
    used_week = await _adds_sent(team_id, week["start_date"], week["end_date"]) if week else 0
    recent = await _recent(team_id, 8)
    return {
        "acting_on": autonomy.acting_rows(autopilot),
        "autopilot": autonomy.switch_on(autopilot),
        "policy": {
            "adds_per_day": settings.autopilot_adds_per_day,
            "adds_per_matchup": settings.autopilot_adds_per_matchup,
            "adds_kept_for_the_user": settings.autopilot_adds_reserve,
            "never_drops_top": settings.autopilot_protected_top,
            "never_drops_added_within_hours": settings.autopilot_churn_hours,
        },
        "your_adds_today": used_today,
        "your_adds_this_matchup": used_week,
        "recent_actions": [{"date": a.nba_date.isoformat(), "kind": a.kind, "outcome": a.outcome, "rule": a.rule,
                            "message": a.message} for a in recent],
    }
