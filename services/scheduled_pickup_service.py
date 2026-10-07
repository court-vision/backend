"""
Scheduled pickups: a free-agent add (optionally with a drop) a user asked for on
a later ESPN day, made by a job at the earliest moment the roster can take it.

    schedule       POST   /teams/{id}/pickups              check against today's board and the pool, store
    list_for_team  GET    /teams/{id}/pickups              pending rows + the last week's settled ones
    cancel         DELETE /teams/{id}/pickups/{pickup_id}  a pending row no attempt holds
    execute_due    POST   /jobs/pickups/execute            the pipeline route: claim the due rows, try each

Timing. ESPN counts an acquisition for the day it is made only before that day's
first game, so the first attempt for day D is the first tip-off of D-1 — unless
the player to drop has a game on D-1 (dropping him before it loses that game;
once it starts he is locked), when the first attempt waits for the ESPN day to
roll into D (02:00 ET, `schedule_service.get_nba_today`'s rule). Every attempt
re-reads the board and the pool through a manual transaction's checks
(`RosterTransactionService._validate`) and sends through its chain (`_write`),
audited under source 'scheduled'. A lock, a waiver period or a writer outage
defers the row; a player who is gone settles it as `skipped` — the no-op; D's
first tip-off is the deadline. After the add, the player is seated in day D's
lineup through the lineup-move chain (FUTURE_ROSTER while D is still ahead),
best effort: a seat that cannot be given never undoes an executed pickup.

Sending. A row's add/drop goes out at most once. Right before it is sent, the row
is checked and marked in one step (`_mark_in_flight`): still pending, still this
attempt's claim (`attempts` unchanged — every claim bumps it), no write out yet.
The mark is the write's audit id on the pending row, and the lease is renewed from
the send. Every settle and deferral is fenced the same way, so an attempt that lost
its row changes nothing and reports nothing. A write that got no answer — the
worker died, the writer timed out — keeps its mark, and the next attempt only reads
the board: the player on it settles the row executed (and seats him), his absence
settles it failed `interrupted`. Only a writer that was provably never reached (no
free slot, no connection) clears the mark, so that retry may send.

Cancelling. Refused while an attempt holds the row — claimed (reason
`in_progress`) and inside its lease, or with a write out — so a user told
"cancelled" never sees the move made anyway; the check and the update are one
statement, so a claim cannot slip between them.

The rules (`attempt_window`, `on_refusal`, `pick_seat`) are pure and table-tested;
the repository functions run through `run_db`; the service composes them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional

import httpx
import pytz
from peewee import IntegrityError, fn

from core.errors import AppError, ConflictError, NotFoundError, ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import db, run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.scheduled_pickup import (
    PickupExecuteData,
    PickupExecuteReq,
    PickupExecuteResp,
    PickupResult,
    SchedulePickupReq,
    ScheduledPickup,
    ScheduledPickupListData,
    ScheduledPickupListResp,
    ScheduledPickupPlayer,
    ScheduledPickupResp,
)
from services.daily_actions_service import has_open_seat
from services.fantasy_writer_client import FantasyWriterUnavailable
from services.lineup_editor_service import (
    LineupEditorService,
    RosterWriteBlocked,
    RosterWriteDisabled,
    RosterWriteRejected,
    RosterWriteUnavailable,
    _load_team_for_job,
    _roster_slots,
    planner_players,
    slot_counts_of,
)
from services.lineup_planner import ACTIVE_SLOT_IDS, BENCH_SLOT_ID, Move, validate_moves
from services.lineup_read_service import LineupReadService
from services.providers import get_provider_adapter, unavailable_message
from services.providers.identity import nba_ids_by_espn_id
from services.roster_transaction_service import RosterTransactionInvalid, RosterTransactionService, _moves_json
from utils.espn_helpers import POSITION_MAP, normalize_nba_abbrev

log = get_logger("scheduled_pickup")

PENDING, EXECUTED, SKIPPED, FAILED, CANCELLED, EXPIRED = "pending", "executed", "skipped", "failed", "cancelled", "expired"
DEFERRED = "deferred"
IN_PROGRESS = "in_progress"         # a claimed row's reason while its attempt runs

EASTERN = pytz.timezone("US/Eastern")
ROLLOVER_ET = time(2, 0)            # the ESPN fantasy day starts here (schedule_service.get_nba_today)
RETRY = timedelta(minutes=10)
WAIVER_RETRY = timedelta(minutes=60)
WRITER_RETRY = timedelta(minutes=5)
LEASE = timedelta(minutes=5)        # a claimed row is invisible to the next tick this long
MAX_ATTEMPTS = 48
RECENT_DAYS = 7
ROSTER_FULL_CODE = "TRAN_ROSTER_FULL"


# ------------------------------- errors ------------------------------- #


class ScheduledPickupInvalid(AppError):
    """The board or the pool refuses the pickup before it is stored. `data.reason` is one
    of: not_future, no_scoring_period, same_player, add_already_on_roster, add_not_found,
    add_not_available, drop_not_on_roster."""

    status_code = 422
    api_status = ApiStatus.VALIDATION_ERROR
    error_code = "SCHEDULED_PICKUP_INVALID"
    default_message = "That pickup cannot be scheduled"
    log_level = "info"


class ScheduledPickupDuplicate(ConflictError):
    error_code = "SCHEDULED_PICKUP_DUPLICATE"
    default_message = "A pickup for that player is already scheduled"


class ScheduledPickupNotPending(ConflictError):
    error_code = "SCHEDULED_PICKUP_NOT_PENDING"
    default_message = "That pickup is no longer pending"


class ScheduledPickupInProgress(ConflictError):
    error_code = "SCHEDULED_PICKUP_IN_PROGRESS"
    default_message = "That pickup is being attempted right now and cannot be cancelled — check back in a few minutes"


class ClaimLost(Exception):
    """The row stopped being this attempt's before its write was sent — the user cancelled it, or
    a later run claimed it once the lease ran out. Nothing is sent, and the attempt reports nothing."""


def _invalid(reason: str, message: str, player_id: Optional[int] = None) -> ScheduledPickupInvalid:
    return ScheduledPickupInvalid(message=message, data={"reason": reason, "player_id": player_id})


def _never_sent(exc: BaseException) -> bool:
    """A writer outage that provably happened before the request left this process — no free
    writer slot, no connection to the writer — as fantasy_writer_client raises it (`from` its
    cause). Anything else (a timeout, a writer 5xx) may have reached ESPN."""
    writer_error = exc.__cause__
    return (isinstance(writer_error, FantasyWriterUnavailable)
            and isinstance(writer_error.__cause__, (TimeoutError, httpx.ConnectError, httpx.ConnectTimeout,
                                                     httpx.PoolTimeout)))


# ------------------------------- rules (pure) ------------------------------- #


def et_at(d: date, t: time) -> datetime:
    """A wall-clock Eastern moment on `d`, as a UTC-aware datetime."""
    return EASTERN.localize(datetime.combine(d, t)).astimezone(timezone.utc)


def rollover_at(d: date) -> datetime:
    """When the ESPN fantasy day `d` begins (02:00 ET), as UTC."""
    return et_at(d, ROLLOVER_ET)


def _lease_from_now(now: datetime) -> datetime:
    """A lease that runs LEASE from this moment in real time: `now` is the run's clock, read at its
    start, and rows earlier in the batch may have used up the claim's lease. Never shorter than
    one from `now` (the dogfood override may be ahead of the wall clock)."""
    return max(now, datetime.now(timezone.utc)) + LEASE


@dataclass(frozen=True)
class AttemptWindow:
    not_before_at: datetime
    deadline_at: Optional[datetime]
    rule: str                        # first_tip_prev | rollover_into_day | rollover_into_prev


def attempt_window(
    nba_date: date,
    drop_team: Optional[str],
    *,
    first_tip: Callable[[date], Optional[time]],
    teams_playing: Callable[[date], set[str]],
) -> AttemptWindow:
    """When the executor first tries a pickup for `nba_date` (day D), and when it gives up.

    The first attempt is D-1's first tip-off, after which an ESPN acquisition counts for
    D. When the player to drop plays on D-1 that would cost his game (or be refused once
    he is locked), so the attempt waits for the rollover into D. A D-1 without games
    means the rollover into D-1. The deadline is D's own first tip-off; None when the
    schedule has no row for D, and the executor then expires the row once the board's
    day has passed D.
    """
    prev = nba_date - timedelta(days=1)
    playing = {normalize_nba_abbrev(t) for t in teams_playing(prev)} if drop_team else set()
    if drop_team and normalize_nba_abbrev(drop_team) in playing:
        start, rule = rollover_at(nba_date), "rollover_into_day"
    else:
        tip = first_tip(prev)
        if tip:
            start, rule = et_at(prev, tip), "first_tip_prev"
        else:
            start, rule = rollover_at(prev), "rollover_into_prev"
    tip_d = first_tip(nba_date)
    deadline = et_at(nba_date, tip_d) if tip_d else None
    return AttemptWindow(not_before_at=start, deadline_at=deadline, rule=rule)


@dataclass(frozen=True)
class Decision:
    action: str                      # settle | defer | add_only
    status: Optional[str] = None     # settle: the row's final status
    reason: Optional[str] = None     # settle / add_only: why
    after: Optional[timedelta] = None  # defer: relative
    at: Optional[datetime] = None      # defer: absolute (the rollover)


def on_refusal(reason: str, *, period: int, day: int, nba_date: date, seat_free: bool) -> Decision:
    """What a pre-send refusal (`RosterTransactionInvalid.data.reason`) means for a scheduled row
    attempted on ESPN day `period` for day `day`."""
    before_day = period < day
    if reason == "add_locked":
        if before_day:
            return Decision("defer", at=rollover_at(nba_date))   # locks clear when the day rolls
        return Decision("settle", EXPIRED, "locked_on_day")       # his game on D already started
    if reason == "drop_locked":
        if before_day:
            return Decision("defer", at=rollover_at(nba_date))
        return Decision("add_only", reason="drop_locked") if seat_free else Decision("settle", SKIPPED, "drop_locked")
    if reason == "add_on_waivers":
        return Decision("defer", after=WAIVER_RETRY)
    if reason in ("add_not_found", "add_not_available"):
        return Decision("settle", SKIPPED, "unavailable")           # the no-op
    if reason == "add_already_on_roster":
        return Decision("settle", SKIPPED, "already_on_roster")
    if reason == "drop_not_on_roster":
        return Decision("add_only", reason="drop_missing") if seat_free else Decision("settle", SKIPPED, "drop_missing")
    return Decision("settle", FAILED, reason)                       # nothing_to_do / same_player: not storable


def pick_seat(state: LineupState, player: LineupPlayer, preferred: Optional[int]) -> Optional[int]:
    """The active slot to give `player` on `state`'s board: the one he was scheduled to
    replace when it is open and he is eligible, else the first eligible active slot with
    room (lowest id first, so UT stays free). None when nothing is open."""
    counts = slot_counts_of(state)
    taken = Counter(p.lineup_slot_id for p in state.players if p.player_id != player.player_id)
    eligible = {s for s in player.eligible_slot_ids if s in ACTIVE_SLOT_IDS}

    def is_open(slot: int) -> bool:
        return slot in eligible and taken.get(slot, 0) < int(counts.get(slot, 0) or 0)

    if preferred is not None and is_open(preferred):
        return preferred
    for slot in sorted(eligible):
        if is_open(slot):
            return slot
    return None


# ------------------------------- DB (run in the executor) ------------------------------- #


def _model():
    from db.models.scheduled_pickups import ScheduledPickup as Row
    return Row


def _due_clause(Row, now: datetime):
    return ((Row.status == PENDING) & (Row.not_before_at <= now)
            & (Row.next_attempt_at.is_null(True) | (Row.next_attempt_at <= now)))


def _claim_due(now: datetime, limit: int) -> list:
    """Lease up to `limit` due rows to this run: bump attempts, push next_attempt_at by the
    lease, mark the attempt running (reason IN_PROGRESS, which a cancel waits out), return the
    rows (soonest first). A concurrent run sees nothing until the lease ends."""
    Row = _model()
    due = _due_clause(Row, now)
    ids = [r.id for r in Row.select(Row.id).where(due).order_by(Row.not_before_at, Row.id).limit(limit)]
    if not ids:
        return []
    rows = list(Row.update(next_attempt_at=now + LEASE, attempts=Row.attempts + 1, last_attempt_at=now,
                           reason=IN_PROGRESS, updated_at=now)
                .where(due & Row.id.in_(ids)).returning(Row).execute())
    return sorted(rows, key=lambda r: (r.not_before_at, r.id))


def _release_due(now: datetime, until: datetime) -> int:
    """Roster writes are off: push every due row back without claiming or counting an attempt."""
    Row = _model()
    return Row.update(next_attempt_at=until, reason="writes_disabled", updated_at=now).where(_due_clause(Row, now)).execute()


def _held(Row, pickup_id: int, attempts: int):
    """The row is still the attempt's that claimed it: pending (no cancel landed) and not claimed
    again since — every claim bumps `attempts`, which makes it the claim's fencing token."""
    return (Row.id == pickup_id) & (Row.status == PENDING) & (Row.attempts == attempts)


def _mark_in_flight(pickup_id: int, attempts: int, audit_id: int, now: datetime, lease_until: datetime) -> bool:
    """The last check before a write is sent, and its record: store the write's audit id on the
    row while it is still this attempt's and no write is out yet. True = send. On a pending row
    the audit id is the in-flight mark the next attempt confirms from the board; the renewed
    lease keeps every other run off the row while the write is out."""
    Row = _model()
    return bool(Row.update(audit_id=audit_id, next_attempt_at=lease_until, updated_at=now)
                .where(_held(Row, pickup_id, attempts) & Row.audit_id.is_null(True)).execute())


def _settle_row(pickup_id: int, attempts: int, now: datetime, *, status: str, reason: Optional[str] = None,
                detail: Optional[str] = None, audit_id: Optional[int] = None,
                lineup_audit_id: Optional[int] = None, seated_slot_id: Optional[int] = None,
                executed_at: Optional[datetime] = None) -> bool:
    """False when the row is no longer this attempt's (nothing changed)."""
    Row = _model()
    return bool(Row.update(status=status, reason=reason, detail=detail, audit_id=audit_id,
                           lineup_audit_id=lineup_audit_id, seated_slot_id=seated_slot_id,
                           executed_at=executed_at, next_attempt_at=None, updated_at=now)
                .where(_held(Row, pickup_id, attempts)).execute())


def _defer_row(pickup_id: int, attempts: int, now: datetime, when: datetime, reason: str,
               detail: Optional[str], audit_id: Optional[int]) -> bool:
    """`audit_id` stays on a row whose write is out (the next attempt confirms it) and is None
    otherwise. False when the row is no longer this attempt's (nothing changed)."""
    Row = _model()
    return bool(Row.update(next_attempt_at=when, reason=reason, detail=detail, audit_id=audit_id, updated_at=now)
                .where(_held(Row, pickup_id, attempts)).execute())


def _insert_pickup(**fields):
    Row = _model()
    try:
        with db.atomic():
            return Row.create(**fields)
    except IntegrityError as exc:
        raise ScheduledPickupDuplicate() from exc


def _list_pickups(team_id: int, since: datetime) -> list:
    Row = _model()
    return list(Row.select()
                .where((Row.team == team_id) & ((Row.status == PENDING) | (Row.updated_at >= since)))
                .order_by(Row.created_at.desc()))


def _cancel_pickup(team_id: int, pickup_id: int, now: datetime):
    """(prior status, row) for the team's row, or None when it is not theirs. A pending row an
    attempt holds is left as it is, prior IN_PROGRESS: claimed and inside its lease (a dead
    attempt's ran out), or with a write out, whose outcome only the next attempt can learn."""
    Row = _model()
    row = Row.get_or_none((Row.id == pickup_id) & (Row.team == team_id))
    if row is None:
        return None
    if row.status != PENDING:
        return row.status, row
    no_attempt_running = ((fn.COALESCE(Row.reason, "") != IN_PROGRESS) | Row.next_attempt_at.is_null(True)
                          | (Row.next_attempt_at <= now))
    changed = (Row.update(status=CANCELLED, next_attempt_at=None, updated_at=now)
               .where((Row.id == pickup_id) & (Row.status == PENDING) & Row.audit_id.is_null(True)
                      & no_attempt_running).execute())
    row = Row.get_by_id(pickup_id)
    if changed:
        return PENDING, row
    return (IN_PROGRESS if row.status == PENDING else row.status), row


def _window_for(nba_date: date, drop_team: Optional[str]) -> AttemptWindow:
    from db.models.nba.games import Game
    return attempt_window(nba_date, drop_team,
                          first_tip=Game.get_earliest_game_time_on_date,
                          teams_playing=Game.get_teams_playing_on_date)


def _nba_ids(rows) -> dict[int, int]:
    ids = {r.add_player_id for r in rows} | {r.drop_player_id for r in rows if r.drop_player_id}
    return nba_ids_by_espn_id(sorted(ids))


# ------------------------------- shapes ------------------------------- #


def _player(player_id: Optional[int], name: Optional[str], team: Optional[str],
            nba_ids: Optional[dict[int, int]] = None) -> Optional[ScheduledPickupPlayer]:
    if player_id is None:
        return None
    return ScheduledPickupPlayer(player_id=player_id, name=name or str(player_id), team=team,
                                 nba_player_id=(nba_ids or {}).get(player_id))


def _slot_name(slot_id: Optional[int]) -> Optional[str]:
    return POSITION_MAP.get(slot_id) if slot_id is not None else None


def to_schema(row, nba_ids: Optional[dict[int, int]] = None) -> ScheduledPickup:
    return ScheduledPickup(
        id=row.id, team_id=row.team_id, scoring_period_id=row.scoring_period_id, nba_date=row.nba_date,
        status=row.status, reason=row.reason, detail=row.detail,
        add=_player(row.add_player_id, row.add_name, row.add_team, nba_ids),
        drop=_player(row.drop_player_id, row.drop_name, row.drop_team, nba_ids),
        not_before_at=row.not_before_at, deadline_at=row.deadline_at, next_attempt_at=row.next_attempt_at,
        attempts=row.attempts, audit_id=row.audit_id, lineup_audit_id=row.lineup_audit_id,
        seated_slot_id=row.seated_slot_id, seated_slot=_slot_name(row.seated_slot_id),
        created_at=row.created_at, executed_at=row.executed_at,
    )


@dataclass(frozen=True)
class Seated:
    lineup_audit_id: Optional[int] = None
    slot_id: Optional[int] = None
    note: Optional[str] = None


# ------------------------------- service ------------------------------- #


class ScheduledPickupService:

    # ---- the user's routes ----

    @staticmethod
    async def schedule(team, league_info: LeagueInfo, req: SchedulePickupReq) -> ScheduledPickupResp:
        if not settings.roster_writes_enabled:
            raise RosterWriteDisabled()
        adapter = get_provider_adapter(league_info.provider)
        if not adapter.capabilities(league_info).transactions:
            raise RosterWriteBlocked(message=unavailable_message("roster_changes", league_info.provider),
                                     data={"reason": "provider_not_supported"})

        slots = _roster_slots(team)
        state = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots)
        if not state.can_write:
            raise RosterWriteBlocked(data={"reason": state.write_blocked_reason})
        current = state.current_scoring_period_id or state.scoring_period_id
        if current is None or not state.nba_date:
            raise _invalid("no_scoring_period", "ESPN reports no current day for this league")
        day = req.scoring_period_id
        if day <= current:
            raise _invalid("not_future", f"Day {day} is not after today (day {current}) — use a roster "
                                         "transaction for today")
        # Bounds are the lineup reader's: today through the season's last day, else 400.
        _, nba_date = LineupReadService._requested_period(day, current, date.fromisoformat(state.nba_date),
                                                          state.final_scoring_period_id)

        add, drop = req.add_player_id, req.drop_player_id
        try:
            holder, pool_entry = await RosterTransactionService._validate(league_info, state, add, drop,
                                                                          for_future_day=True)
        except RosterTransactionInvalid as exc:
            raise ScheduledPickupInvalid(message=exc.message, data=exc.data) from exc
        assert pool_entry is not None  # `add` is required, so _validate looked him up

        window = await run_db("pickup.window", _window_for, nba_date, holder.team if holder else None)
        now = datetime.now(timezone.utc)
        row = await run_db(
            "pickup.insert", _insert_pickup,
            user=team.user_id, team=team.team_id, add_player_id=add, drop_player_id=drop,
            add_name=pool_entry.name, add_team=pool_entry.pro_team,
            drop_name=holder.name if holder else None, drop_team=holder.team if holder else None,
            scoring_period_id=day, nba_date=nba_date, not_before_at=window.not_before_at,
            deadline_at=window.deadline_at, status=PENDING, created_at=now, updated_at=now,
        )
        log.info("scheduled_pickup_created", team_id=team.team_id, pickup_id=row.id, add=add, drop=drop,
                 day=day, nba_date=nba_date.isoformat(), rule=window.rule,
                 not_before_at=window.not_before_at.isoformat())
        when = window.not_before_at.astimezone(EASTERN).strftime("%a %b %-d, %-I:%M %p ET")
        return ScheduledPickupResp(
            status=ApiStatus.SUCCESS,
            message=f"Pickup of {pool_entry.name} scheduled for ESPN day {day} — first attempt {when}",
            data=to_schema(row),
        )

    @staticmethod
    async def list_for_team(team) -> ScheduledPickupListResp:
        since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
        rows = await run_db("pickup.list", _list_pickups, team.team_id, since)
        nba_ids = await run_db("pickup.nba_ids", _nba_ids, rows) if rows else {}
        pending = sorted((r for r in rows if r.status == PENDING), key=lambda r: (r.nba_date, r.id))
        recent = sorted((r for r in rows if r.status != PENDING), key=lambda r: r.updated_at, reverse=True)
        return ScheduledPickupListResp(
            status=ApiStatus.SUCCESS,
            message=f"{len(pending)} pending pickup(s)",
            data=ScheduledPickupListData(pending=[to_schema(r, nba_ids) for r in pending],
                                         recent=[to_schema(r, nba_ids) for r in recent]),
        )

    @staticmethod
    async def cancel(team, pickup_id: int) -> ScheduledPickupResp:
        found = await run_db("pickup.cancel", _cancel_pickup, team.team_id, pickup_id, datetime.now(timezone.utc))
        if found is None:
            raise NotFoundError("SCHEDULED_PICKUP_NOT_FOUND", "Scheduled pickup not found")
        prior, row = found
        if prior == IN_PROGRESS:
            raise ScheduledPickupInProgress(data={"status": row.status, "pickup": to_schema(row).model_dump(mode="json")})
        if prior != PENDING:
            raise ScheduledPickupNotPending(data={"status": row.status, "pickup": to_schema(row).model_dump(mode="json")})
        log.info("scheduled_pickup_cancelled", team_id=team.team_id, pickup_id=pickup_id)
        return ScheduledPickupResp(status=ApiStatus.SUCCESS, message=f"Pickup of {row.add_name} cancelled",
                                   data=to_schema(row))

    # ---- the pipeline route ----

    @staticmethod
    async def execute_due(req: PickupExecuteReq) -> PickupExecuteResp:
        now = (req.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not settings.roster_writes_enabled:
            pushed = await run_db("pickup.release", _release_due, now, now + RETRY)
            log.info("scheduled_pickups_writes_disabled", due=pushed)
            return PickupExecuteResp(
                status=ApiStatus.SUCCESS,
                message=f"Roster writes are switched off — {pushed} due pickup(s) pushed back {RETRY.seconds // 60} min",
                data=PickupExecuteData(due=pushed, results=[]),
            )
        rows = await run_db("pickup.claim", _claim_due, now, req.limit)
        results: list[PickupResult] = []
        for row in rows:
            try:
                result = await ScheduledPickupService._execute_one(row, now)
            except Exception as exc:  # one broken row must not take the batch down
                log.exception("scheduled_pickup_error", pickup_id=row.id, team_id=row.team_id, error=str(exc))
                result = await ScheduledPickupService._defer(row, now, "error", detail=str(exc)[:300])
            if result is not None:    # None: the row stopped being this run's, and its holder reports it
                results.append(result)
        log.info("scheduled_pickups_run", due=len(rows),
                 outcomes=dict(Counter(r.outcome for r in results)))
        return PickupExecuteResp(status=ApiStatus.SUCCESS, message=f"{len(rows)} pickup(s) attempted",
                                 data=PickupExecuteData(due=len(rows), results=results))

    @staticmethod
    async def _execute_one(row, now: datetime) -> Optional[PickupResult]:
        """One attempt at a claimed row. None when the row stopped being this attempt's under it
        (cancelled, or claimed by a later run once the lease ran out): nothing is sent then."""
        day, nba_date = row.scoring_period_id, row.nba_date
        settle, defer = ScheduledPickupService._settle, ScheduledPickupService._defer

        try:
            league_info, slots = await run_db("pickup.job_team", _load_team_for_job, row.team_id, row.user_id)
        except NotFoundError:
            return await settle(row, now, FAILED, "team_not_found")
        team_name = league_info.team_name or ""
        adapter = get_provider_adapter(league_info.provider)
        if not adapter.capabilities(league_info).transactions:
            return await settle(row, now, FAILED, "provider_not_supported", team_name=team_name)

        try:
            state = await adapter.read_lineup(row.team_id, league_info, fallback_slot_counts=slots)
        except ProviderAuthError:
            return await settle(row, now, FAILED, "auth_expired", team_name=team_name)
        except Exception as exc:
            return await defer(row, now, "provider_error", detail=str(exc)[:300], team_name=team_name)

        if row.audit_id is not None:
            # A write went out on an earlier attempt and was never settled — the worker died, or
            # the writer gave no answer. It is never sent again: the board says whether it landed.
            return await ScheduledPickupService._confirm_sent(row, now, state, league_info, slots, adapter,
                                                              team_name=team_name)

        period = state.scoring_period_id
        if not state.can_write:
            blocked = f"can_write:{state.write_blocked_reason}"
            if state.write_blocked_reason in ("no_scoring_period", "writes_disabled"):
                return await defer(row, now, blocked, team_name=team_name)
            return await settle(row, now, FAILED, blocked, team_name=team_name)
        if period is None:
            return await defer(row, now, "no_scoring_period", team_name=team_name)
        if period > day:
            return await settle(row, now, EXPIRED, "day_passed", team_name=team_name)
        if row.deadline_at is not None and now >= row.deadline_at:
            return await settle(row, now, EXPIRED, "deadline", team_name=team_name)
        if period < day - 1:
            return await defer(row, now, "period_lag", team_name=team_name)

        add, drop = row.add_player_id, row.drop_player_id
        on_board = {p.player_id: p for p in state.players}
        seat_free = has_open_seat(state)
        note: Optional[str] = None

        if add in on_board:
            # Already here with no write of ours out (one that is out was confirmed above): the
            # user picked him up by hand. Nothing is sent.
            return await settle(row, now, SKIPPED, "already_on_roster", team_name=team_name)

        holder = on_board.get(drop) if drop is not None else None
        if drop is not None and holder is None:
            if not seat_free:
                return await settle(row, now, SKIPPED, "drop_missing", team_name=team_name)
            drop, note = None, "drop_missing"
        elif holder is not None and period < day and holder.has_game_today:
            # Dropping him before his game tonight loses it; after it starts he is locked.
            return await defer(row, now, "holder_plays_today", at=rollover_at(nba_date), team_name=team_name)

        try:
            holder, pool_entry = await RosterTransactionService._validate(league_info, state, add, drop)
        except RosterTransactionInvalid as exc:
            decision = on_refusal(exc.data["reason"], period=period, day=day, nba_date=nba_date, seat_free=seat_free)
            if decision.action == "defer":
                return await defer(row, now, exc.data["reason"], after=decision.after, at=decision.at,
                                   detail=exc.message, team_name=team_name)
            if decision.action == "settle":
                return await settle(row, now, decision.status, decision.reason, detail=exc.message, team_name=team_name)
            drop, note = None, decision.reason                       # add_only
            try:
                holder, pool_entry = await RosterTransactionService._validate(league_info, state, add, None)
            except RosterTransactionInvalid as exc2:
                second = on_refusal(exc2.data["reason"], period=period, day=day, nba_date=nba_date, seat_free=False)
                if second.action == "defer":
                    return await defer(row, now, exc2.data["reason"], after=second.after, at=second.at,
                                       detail=exc2.message, team_name=team_name)
                return await settle(row, now, second.status or FAILED, second.reason, detail=exc2.message,
                                    team_name=team_name)
        assert pool_entry is not None

        # Where he should sit on day D: the seat of the player he replaces, read before the add changes it.
        preferred = await ScheduledPickupService._holder_slot_on_day(row, league_info, slots, adapter, holder, period)

        async def last_check(audit_id: int) -> None:
            # Right before the write leaves: the row must still be this attempt's, and takes the
            # in-flight mark, or nothing is sent.
            lease_until = _lease_from_now(now)
            if not await run_db("pickup.in_flight", _mark_in_flight, row.id, row.attempts, audit_id, now, lease_until):
                raise ClaimLost("the pickup was cancelled or claimed by another run before its write was sent")
            row.audit_id, row.next_attempt_at = audit_id, lease_until

        names = {p.player_id: p.name for p in state.players}
        names[pool_entry.player_id] = pool_entry.name
        try:
            fresh, verified, audit_id = await RosterTransactionService._write(
                user_id=row.user_id, team_id=row.team_id, league_info=league_info, state=state,
                add=add, drop=drop, moves=_moves_json(add, drop, names), fallback_slot_counts=slots,
                source="scheduled", before_send=last_check,
            )
        except ClaimLost:
            log.warning("scheduled_pickup_claim_lost", pickup_id=row.id, team_id=row.team_id, step="send")
            return None
        except RosterWriteRejected as exc:
            code = (exc.data or {}).get("espn_error_code")
            if code == ROSTER_FULL_CODE:
                return await settle(row, now, SKIPPED, "roster_full", detail=exc.message, team_name=team_name)
            return await settle(row, now, FAILED, "espn_rejected", detail=exc.message, team_name=team_name)
        except ProviderAuthError:
            return await settle(row, now, FAILED, "auth_expired", team_name=team_name)
        except RosterWriteUnavailable as exc:
            if _never_sent(exc):
                row.audit_id = None     # the writer was never reached: the retry may send
            # Otherwise the write may have reached ESPN: it keeps its mark, and the retry only reads the board.
            return await defer(row, now, "writer_unavailable", after=WRITER_RETRY, detail=exc.message,
                               team_name=team_name)

        log.info("scheduled_pickup_executed", pickup_id=row.id, team_id=row.team_id, add=add, drop=drop,
                 day=day, period=period, verified=verified, audit_id=audit_id, note=note)
        seated = await ScheduledPickupService._seat_on_day(row, league_info, slots, adapter,
                                                           period=period, preferred=preferred)
        return await settle(row, now, EXECUTED, note if note else (None if verified else "unverified"),
                            audit_id=audit_id, seated=seated, verified=verified, team_name=team_name)

    @staticmethod
    async def _holder_slot_on_day(row, league_info, slots, adapter, holder: Optional[LineupPlayer],
                                  period: int) -> Optional[int]:
        if holder is None:
            return None
        if row.scoring_period_id == period:
            return holder.lineup_slot_id
        try:
            board = await adapter.read_lineup(row.team_id, league_info, fallback_slot_counts=slots,
                                              scoring_period_id=row.scoring_period_id)
        except Exception as exc:
            log.warning("scheduled_pickup_day_board_unavailable", pickup_id=row.id, error=str(exc))
            return None
        return next((p.lineup_slot_id for p in board.players if p.player_id == holder.player_id), None)

    @staticmethod
    async def _confirm_sent(row, now: datetime, state: LineupState, league_info, slots, adapter, *,
                            team_name: str) -> Optional[PickupResult]:
        """Settle a row whose write went out on an earlier attempt, from today's board alone — never
        by sending again, since a write that got no answer may still have reached ESPN. The player on
        the board: executed, and seated on day D unless D has passed (or the board is read-only). Not
        on it: the write never landed — failed `interrupted`, and the user makes the move by hand."""
        on_board = {p.player_id for p in state.players}
        if row.add_player_id not in on_board:
            return await ScheduledPickupService._settle(
                row, now, FAILED, "interrupted", team_name=team_name,
                detail="A write for this pickup went out with no answer recorded, and the roster does not "
                       "show it; it is not sent again")
        period = state.scoring_period_id
        seated = Seated()
        if state.can_write and period is not None and period <= row.scoring_period_id:
            seated = await ScheduledPickupService._seat_on_day(row, league_info, slots, adapter,
                                                               period=period, preferred=None)
        dropped = row.drop_player_id is None or row.drop_player_id not in on_board
        return await ScheduledPickupService._settle(row, now, EXECUTED, "already_rostered", seated=seated,
                                                    verified=dropped, team_name=team_name)

    @staticmethod
    async def _seat_on_day(row, league_info, slots, adapter, *, period: int, preferred: Optional[int]) -> Seated:
        """Move the new player from the bench into an active slot for day D. Best effort:
        every failure is a note on the row, never a change to the pickup's outcome."""
        day = row.scoring_period_id
        try:
            board = await adapter.read_lineup(row.team_id, league_info, fallback_slot_counts=slots,
                                              scoring_period_id=day if day > period else None)
            me = next((p for p in board.players if p.player_id == row.add_player_id), None)
            if me is None:
                return Seated(note="seat:not_on_board")
            if me.lineup_slot_id != BENCH_SLOT_ID:
                return Seated(slot_id=me.lineup_slot_id)                 # ESPN seated him itself
            target = pick_seat(board, me, preferred)
            if target is None:
                return Seated(note="seat:no_open_slot")
            moves = [Move(me.player_id, BENCH_SLOT_ID, target, role="start")]
            errors = validate_moves(planner_players(board), slot_counts_of(board), moves)
            if errors:
                return Seated(note=f"seat:{errors[0].code}")
            _, verified, audit_id = await LineupEditorService._write(
                user_id=row.user_id, team_id=row.team_id, league_info=league_info, state=board,
                moves=moves, source="scheduled", fallback_slot_counts=slots,
            )
            log.info("scheduled_pickup_seated", pickup_id=row.id, team_id=row.team_id, slot=target,
                     day=day, future=board.future, verified=verified, audit_id=audit_id)
            return Seated(lineup_audit_id=audit_id, slot_id=target, note=None if verified else "seat:unverified")
        except AppError as exc:
            log.warning("scheduled_pickup_seat_refused", pickup_id=row.id, code=exc.error_code, error=exc.message)
            return Seated(note=f"seat:{exc.error_code}: {exc.message}"[:300])
        except Exception as exc:
            log.warning("scheduled_pickup_seat_failed", pickup_id=row.id, error=str(exc))
            return Seated(note=f"seat:error: {exc}"[:300])

    # ---- settling ----

    @staticmethod
    def _result(row, outcome: str, reason: Optional[str], *, detail: Optional[str] = None,
                audit_id: Optional[int] = None, seated_slot: Optional[str] = None,
                verified: Optional[bool] = None, next_attempt_at: Optional[datetime] = None,
                team_name: str = "") -> PickupResult:
        return PickupResult(
            pickup_id=row.id, team_id=row.team_id, user_id=row.user_id, team_name=team_name,
            outcome=outcome, reason=reason, detail=detail,
            add=_player(row.add_player_id, row.add_name, row.add_team),
            drop=_player(row.drop_player_id, row.drop_name, row.drop_team),
            nba_date=row.nba_date, scoring_period_id=row.scoring_period_id,
            seated_slot=seated_slot, verified=verified, audit_id=audit_id, next_attempt_at=next_attempt_at,
        )

    @staticmethod
    async def _settle(row, now: datetime, status: str, reason: Optional[str], *, detail: Optional[str] = None,
                      audit_id: Optional[int] = None, seated: Optional[Seated] = None,
                      verified: Optional[bool] = None, team_name: str = "") -> Optional[PickupResult]:
        """None when the row is no longer this attempt's: nothing is changed or reported. A write
        that went out keeps its audit id on the row whatever the outcome."""
        seated = seated or Seated()
        audit_id = audit_id if audit_id is not None else row.audit_id
        if seated.note:
            detail = f"{detail}; {seated.note}" if detail else seated.note
        if not await run_db("pickup.settle", _settle_row, row.id, row.attempts, now, status=status, reason=reason,
                            detail=detail, audit_id=audit_id, lineup_audit_id=seated.lineup_audit_id,
                            seated_slot_id=seated.slot_id, executed_at=now if status == EXECUTED else None):
            log.warning("scheduled_pickup_claim_lost", pickup_id=row.id, team_id=row.team_id, step="settle",
                        status=status, reason=reason)
            return None
        log.info("scheduled_pickup_settled", pickup_id=row.id, team_id=row.team_id, status=status, reason=reason,
                 seated_slot=seated.slot_id, attempts=row.attempts)
        return ScheduledPickupService._result(row, status, reason, detail=detail, audit_id=audit_id,
                                              seated_slot=_slot_name(seated.slot_id), verified=verified,
                                              team_name=team_name)

    @staticmethod
    async def _defer(row, now: datetime, reason: str, *, after: Optional[timedelta] = None,
                     at: Optional[datetime] = None, detail: Optional[str] = None,
                     team_name: str = "") -> Optional[PickupResult]:
        """Keep the row pending and try again at `at`, or after `after` (default RETRY) — unless
        that is past the deadline or the attempt budget, which settles it instead. A row whose
        write is out keeps its mark, is retried no sooner than the lease its send renewed, and is
        not expired by the deadline: the retry only reads the board, and D's first tip-off does
        not change whether the write landed. None when the row is no longer this attempt's."""
        when = max(at, now + RETRY) if at is not None else now + (after or RETRY)
        in_flight = row.audit_id is not None
        if in_flight and row.next_attempt_at is not None:
            when = max(when, row.next_attempt_at)
        if not in_flight and row.deadline_at is not None and when >= row.deadline_at:
            return await ScheduledPickupService._settle(row, now, EXPIRED, "deadline", detail=f"{reason}: {detail}" if detail else reason,
                                                        team_name=team_name)
        if row.attempts >= MAX_ATTEMPTS:
            return await ScheduledPickupService._settle(row, now, FAILED, "max_attempts", detail=f"{reason}: {detail}" if detail else reason,
                                                        team_name=team_name)
        if not await run_db("pickup.defer", _defer_row, row.id, row.attempts, now, when, reason, detail, row.audit_id):
            log.warning("scheduled_pickup_claim_lost", pickup_id=row.id, team_id=row.team_id, step="defer",
                        reason=reason)
            return None
        log.info("scheduled_pickup_deferred", pickup_id=row.id, team_id=row.team_id, reason=reason,
                 next_attempt_at=when.isoformat(), attempts=row.attempts)
        return ScheduledPickupService._result(row, DEFERRED, reason, detail=detail, next_attempt_at=when,
                                              team_name=team_name)
