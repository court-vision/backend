"""
Stream lanes: a standing order to keep one roster spot streaming for the rest of
the matchup (docs/STREAM_LANE_PLAN.md). A team can stream up to `MAX_LANES` spots,
one lane each.

    plan           POST   /teams/{id}/stream-lanes/plan       what a lane started now would do (a dry run)
    start          POST   /teams/{id}/stream-lanes            start one on a spot
    list_for_team  GET    /teams/{id}/stream-lanes            the open lanes, and the last week's ended ones
    update         PATCH  /teams/{id}/stream-lanes/{lane}     pause, resume, notify mode
    stop           DELETE /teams/{id}/stream-lanes/{lane}     end it
    decide_due     the scheduled-pickups executor's tick      claim the due decisions and make them, by team

A lane is intent, not a list of pickups. What it shows is a projection: the best
plan if nobody else touched the market. What it does is decide each day afresh,
at the moment a pickup for that day can first be made — a scheduled pickup's
timing rule (`scheduled_pickup_service.attempt_window`): the previous day's first
tip-off (the rollover into it when it has no games), after which ESPN takes the
switch for the day and the player dropped plays the previous day out. A decision
re-reads the board, the pool and the adds left, re-plans the rest of the matchup
(`services.multi_stream_planner`), and for each switch the plan makes that day
inserts a scheduled pickup carrying its lane's id; the pickup executor then makes
it like any other (locks, waivers, the roster check, the seat, the email).
Otherwise it records a keep. A projected player who is gone
is not a failure: the plan picks again.

Together. A team's lanes deciding the same day are planned as one: they share the
adds (the budget, a per-day limit) and the lineup's seats, and a free agent goes to
one of them. Their decisions fall due together (the same day's window), and a run
claims all of a team's due lanes at once. A lane that has already decided the day
(started after the others decided it, or waiting on its pickup) is part of the
roster as it stands for the others, with its pending pickup made on its day; from
the next day it decides with them.

Holder. An executed lane pickup makes the added player the lane's holder
(`note_pickup_settled`, from the pickup executor's settle). If the holder leaves
the roster any other way the lane pauses and says so. Scheduled pickups and the
desk's transactions refuse to drop an active lane's holder
(`db.models.stream_lanes.active_lane_holder`).

Budget. The planner gets the adds the league leaves (a per-matchup limit's room,
a season cap's) less the pickups already scheduled, and how many switches may
take effect each day (a per-day limit's room once its adds are used or
scheduled). Nothing else: whether to replace a player who plays or wait for
their day off is the planner's to work out.

Values. A player's value on a day is the board's `avg_points` (free agents: the
pool's, the same measure) when their team plays and they aren't out; the planner
turns that into what they add to the day's best lineup.

Fencing. As for pickups: a claim bumps `attempts`, and a decision's writes (the
pickup, the events, the lane's next step) land in one transaction, only while the
lane is still active and still that claim's. A pause or stop in between wins, and
either one cancels the pickup the lane has scheduled (refused while it is being
made); the executor makes a lane's pickup only while the lane is active.

The rules (`first_open_period`, `rehold_after_for`, `lane_budget`, `active_slots`,
`plan_lanes`, `projections_of`, `compare_to_projection`) are pure and table-tested.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from peewee import IntegrityError

from core.compute import run_cpu
from core.errors import AppError, ConflictError, NotFoundError, ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import db, run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.lineup_editor import AcquisitionState, LineupPlayer, LineupState
from schemas.scheduled_pickup import LaneNotice, PickupLane, ScheduledPickupPlayer
from schemas.stream_lane import (
    LanePlayer,
    LaneProjection,
    LaneProjectionDay,
    StreamLane,
    StreamLaneEvent,
    StreamLaneListData,
    StreamLaneListResp,
    StreamLanePlan,
    StreamLanePlanOther,
    StreamLanePlanReq,
    StreamLanePlanResp,
    StreamLaneResp,
    StreamLaneStartReq,
    StreamLaneUpdateReq,
)
from services.acquisitions import adds_for_day, in_current_matchup, matchup_room, season_room
from services.lineup_editor_service import (
    RosterWriteBlocked,
    RosterWriteDisabled,
    _load_team_for_job,
    _roster_slots,
    slot_counts_of,
)
from services.lineup_planner import ACTIVE_SLOT_IDS, IR_SLOT_ID, OUT_STATUSES
from services.providers import get_provider_adapter, unavailable_message
from services.providers.identity import nba_ids_by_espn_id
from services.scheduled_pickup_service import (
    EXECUTED,
    IN_PROGRESS,
    PENDING,
    _cancel_pickup,
    _window_for,
    et_at,
    rollover_at,
)
from services.multi_stream_planner import MultiStreamPlan, Streamer, plan_streams
from services.stream_planner import DayPlayer
from utils.espn_helpers import POSITION_MAP, normalize_nba_abbrev

log = get_logger("stream_lane")

ACTIVE, PAUSED, ENDED = "active", "paused", "ended"
MAX_LANES = 4                        # open lanes per team: they are planned together
FA_COUNT = 300                       # the pool read, as the Week desk's market reads it
RETRY = timedelta(minutes=10)        # a decision that could not be made tries again after this
PICKUP_WAIT = timedelta(minutes=5)   # while the lane's last pickup is still pending
LEASE = timedelta(minutes=5)         # a claimed decision is invisible to the next tick this long
RECENT_DAYS = 7
EVENTS_SHOWN = 60


# ------------------------------- errors ------------------------------- #


class StreamLaneInvalid(AppError):
    """The board, the league or the calendar refuses the lane. `data.reason` is one of:
    not_on_roster, on_ir, scheduled_out, no_matchup_end, matchup_over, no_scoring_period,
    holder_gone."""

    status_code = 422
    api_status = ApiStatus.VALIDATION_ERROR
    error_code = "STREAM_LANE_INVALID"
    default_message = "That spot cannot be streamed"
    log_level = "info"


class StreamLaneExists(ConflictError):
    error_code = "STREAM_LANE_EXISTS"
    default_message = "A stream lane is already streaming that spot"


class StreamLaneLimit(ConflictError):
    error_code = "STREAM_LANE_LIMIT"
    default_message = f"A team can stream up to {MAX_LANES} spots at once — stop one before starting another"


class StreamLaneEnded(ConflictError):
    error_code = "STREAM_LANE_ENDED"
    default_message = "That stream lane has ended"


class StreamLaneBusy(ConflictError):
    error_code = "STREAM_LANE_BUSY"
    default_message = ("The lane's pickup is being made right now and cannot be cancelled — "
                       "check back in a few minutes")


def _invalid(reason: str, message: str) -> StreamLaneInvalid:
    return StreamLaneInvalid(message=message, data={"reason": reason})


# ------------------------------- rules (pure) ------------------------------- #


@dataclass(frozen=True)
class LaneDay:
    period: int                      # ESPN scoring period
    nba_date: date
    teams: frozenset[str]            # normalized NBA tricodes with a game that day
    first_tip: Optional[datetime]    # UTC; None when nobody plays


@dataclass(frozen=True)
class PoolPlayer:
    """A free agent the lane could take."""
    player_id: int
    name: str
    team: str                        # normalized NBA tricode
    eligible: frozenset[int]         # active slot ids
    value: float                     # per game
    default_position_id: Optional[int] = None
    available_on: Optional[date] = None   # the first day they can hold the spot (waivers); None = now


@dataclass(frozen=True)
class PendingPickup:
    """A pickup already scheduled for the team, which the plan takes as given."""
    pickup_id: int
    period: int
    nba_date: date
    add_player_id: int
    drop_player_id: Optional[int]
    lane_id: Optional[int] = None


@dataclass(frozen=True)
class Budget:
    adds: Optional[int]                       # adds the lanes may plan with, together; None = no limit
    day_limits: tuple[Optional[int], ...]     # per day: the most switches that may take effect (None: any)


def first_open_period(today: LaneDay, now: datetime) -> int:
    """The first ESPN day an add made now counts for: today until its first tip-off, then
    tomorrow (ESPN puts an add made after the day's first game on the next day)."""
    return today.period + 1 if today.first_tip is not None and now >= today.first_tip else today.period


def rehold_after_for(acq: Optional[AcquisitionState]) -> int:
    """Days before a player the lane drops can hold the spot again, from the league's waiver
    period. A switch for day s is made at day s-1's first tip-off (or the rollover into s-1);
    the player dropped clears waivers about `waiver_hours` later, around day s+1's decision point
    with 24 hours, so they can be taken back from day s+2. A league that sends no waiver period
    is taken as 24 hours."""
    hours = 24 if acq is None or acq.waiver_hours is None else acq.waiver_hours
    return 1 if hours <= 0 else 1 + math.ceil(hours / 24)


def lane_budget(acq: Optional[AcquisitionState], days: Sequence[LaneDay], pending: Sequence[PendingPickup], *,
                first_day_adds: Optional[int] = None) -> Budget:
    """The adds the lanes may plan with, and how many switches may take effect each day. Pending
    pickups draw on the same allowances; `first_day_adds` is what a per-day limit has already
    counted for the first day (None when unread: ESPN refuses an over-limit add itself)."""
    n = len(days)
    if acq is None:
        return Budget(None, (None,) * n)
    rooms: list[int] = []
    room = matchup_room(acq)
    if room is not None:
        rooms.append(room - sum(1 for p in pending if in_current_matchup(acq, p.nba_date)))
    room = season_room(acq)
    if room is not None:
        rooms.append(room - len(pending))
    adds = max(0, min(rooms)) if rooms else None
    day_limits: list[Optional[int]] = [None] * n
    if acq.per == "day" and acq.limit is not None:
        for i, day in enumerate(days):
            used = sum(1 for p in pending if p.period == day.period)
            if i == 0 and first_day_adds:
                used += first_day_adds
            day_limits[i] = max(0, acq.limit - used)
    return Budget(adds, tuple(day_limits))


# ESPN's combo slots, from the base positions the pool keeps.
_COMBOS = {7: {"SG", "SF"}, 8: {"PG", "SG", "SF", "PF", "G", "F"}, 9: {"PF", "C"}, 10: {"SF", "PF", "C", "F"}}
_BASE = ("PG", "SG", "SF", "PF", "C", "G", "F")
UTIL_SLOT_ID = 11


def active_slots(valid_positions: Iterable[str]) -> frozenset[int]:
    """The active slots a pool player may fill. The pool keeps base positions (PG … C, G, F) and
    UT; the combo slots follow from them, and every player can fill UT."""
    names = {p.upper() for p in valid_positions}
    slots = {POSITION_MAP[n] for n in _BASE if n in names}
    slots |= {slot for slot, bases in _COMBOS.items() if names & bases}
    slots.add(UTIL_SLOT_ID)
    return frozenset(slots)


def _is_out(injury_status: Optional[str]) -> bool:
    return (injury_status or "ACTIVE").upper() in OUT_STATUSES


def plan_lanes(board: LineupState, holder_ids: Sequence[int], pool: Sequence[PoolPlayer], days: Sequence[LaneDay],
               pending: Sequence[PendingPickup], budget: Budget, rehold_after: int) -> MultiStreamPlan:
    """The plan for the spots `holder_ids` hold over `days`, together: they share the adds and
    the lineup's seats. The rest of the roster is the board's (IR aside) with the pending pickups
    made on their days; a free agent on the roster or already scheduled is not a candidate. ESPN's
    position limits are counted each day the same way (IR included): the spots' players at a
    limited position fit the room the rest of the roster leaves."""
    slots = slot_counts_of(board)
    spot_ids = set(holder_ids)
    on_board = {p.player_id: p for p in board.players}
    by_id = {f.player_id: f for f in pool}

    def plays(team: Optional[str], day: LaneDay) -> bool:
        return bool(team) and normalize_nba_abbrev(team) in day.teams

    def roster_value(p: LineupPlayer, day: LaneDay) -> float:
        return p.avg_points if plays(p.team, day) and not _is_out(p.injury_status) else 0.0

    def roster_slots(p: LineupPlayer) -> frozenset[int]:
        return frozenset(s for s in p.eligible_slot_ids if s in ACTIVE_SLOT_IDS)

    def pool_value(f: PoolPlayer, day: LaneDay) -> float:
        return f.value if plays(f.team, day) else 0.0

    rest_by_day: list[list[DayPlayer]] = []
    for day in days:
        rest = [DayPlayer(p.player_id, roster_value(p, day), roster_slots(p)) for p in board.players
                if p.player_id not in spot_ids and p.lineup_slot_id != IR_SLOT_ID]
        for pk in pending:
            if pk.period <= day.period:
                rest = [r for r in rest if r.player_id != pk.drop_player_id]
                if pk.add_player_id in by_id:
                    f = by_id[pk.add_player_id]
                    rest.append(DayPlayer(f.player_id, pool_value(f, day), f.eligible))
        rest_by_day.append(rest)

    holders = [Streamer(pid, tuple(roster_value(on_board[pid], d) for d in days), roster_slots(on_board[pid]),
                        position=on_board[pid].default_position_id) for pid in holder_ids]
    taken = {p.player_id for p in board.players} | {pk.add_player_id for pk in pending}
    candidates: list[Streamer] = []
    for f in pool:
        if f.player_id in taken:
            continue
        taken.add(f.player_id)        # the pool's first entry for a player
        start = 0 if f.available_on is None else max(0, (f.available_on - days[0].nba_date).days)
        if start < len(days):
            candidates.append(Streamer(f.player_id, tuple(pool_value(f, d) for d in days), f.eligible, start,
                                       f.default_position_id))

    limits = {int(k): int(v) for k, v in board.position_limits.items() if int(v) > 0}
    position = {f.player_id: f.default_position_id for f in pool}
    position.update({p.player_id: p.default_position_id for p in board.players})
    room: dict[int, list[int]] = {pos: [] for pos in limits}
    for day in days:
        ids = {p.player_id for p in board.players if p.player_id not in spot_ids}
        for pk in sorted(pending, key=lambda pk: (pk.period, pk.pickup_id)):
            if pk.period <= day.period:
                ids.discard(pk.drop_player_id)
                ids.add(pk.add_player_id)
        held = Counter(position.get(pid) for pid in ids)
        for pos, limit in limits.items():
            room[pos].append(max(0, limit - held[pos]))
    return plan_streams(holders, candidates, rest_by_day, slots, adds=budget.adds, day_limits=budget.day_limits,
                        rehold_after=rehold_after, position_room=room)


def projections_of(plan: MultiStreamPlan, days: Sequence[LaneDay], players: Mapping[int, LanePlayer], *,
                   adds_left: Optional[int], rehold_after: int, now: datetime) -> list[LaneProjection]:
    """Each spot's projection, in the plan's holders' order. A day's value is what the spot's player
    adds to its best lineup with the other spots' players in it; the hold value is the spot's player
    now kept every day, the others as planned."""
    out: list[LaneProjection] = []
    for k, holder_id in enumerate(plan.holder_ids):
        rows: list[LaneProjectionDay] = []
        prev = holder_id
        for i, (day, pid) in enumerate(zip(days, plan.spots[k], strict=True)):
            rows.append(LaneProjectionDay(scoring_period_id=day.period, nba_date=day.nba_date, player=players[pid],
                                          value=round(plan.marginals[k][i], 2), switch=pid != prev))
            prev = pid
        value, hold = sum(plan.marginals[k]), plan.hold_values[k]
        out.append(LaneProjection(as_of=now, days=rows, value=round(value, 2), hold_value=round(hold, 2),
                                  gain=round(value - hold, 2), adds=sum(1 for sw in plan.switches if sw.spot == k),
                                  adds_left=adds_left, rehold_after=rehold_after, lanes=len(plan.holder_ids)))
    return out


def compare_to_projection(projection: Optional[Mapping[str, Any]], period: int, chosen: int, holder_id: int,
                          considered: frozenset[int]) -> tuple[Optional[LanePlayer], Optional[str]]:
    """Who the projection before this decision had holding the spot on `period`, and how the
    decision compares: as_projected; projected_gone (that free agent is no longer one the plan
    could take); or replanned (they are, but the numbers, the schedule or the adds changed)."""
    day = next((d for d in (projection or {}).get("days") or [] if d.get("scoring_period_id") == period), None)
    if day is None:
        return None, None
    who = LanePlayer.model_validate(day["player"])
    if who.player_id == chosen:
        return who, "as_projected"
    if who.player_id != holder_id and who.player_id not in considered:
        return who, "projected_gone"
    return who, "replanned"


# ------------------------------- DB (run in the executor) ------------------------------- #


def _models():
    from db.models.stream_lanes import StreamLane as Lane, StreamLaneEvent as Event
    return Lane, Event


def _pickup_model():
    from db.models.scheduled_pickups import ScheduledPickup as Pickup
    return Pickup


def _days(start_period: int, end_period: int, base_period: int, base_date: date) -> list[LaneDay]:
    """ESPN days start..end with who plays and the first tip-off, dated from the board's day: one
    read of the schedule for the whole range."""
    from db.models.nba.games import Game
    first = base_date + timedelta(days=start_period - base_period)
    last = base_date + timedelta(days=end_period - base_period)
    teams: dict[date, set[str]] = {}
    tips: dict[date, Any] = {}
    games = (Game.select(Game.game_date, Game.home_team_id, Game.away_team_id, Game.start_time_et)
             .where(Game.game_date.between(first, last)))
    for g in games:
        teams.setdefault(g.game_date, set()).update((g.home_team_id, g.away_team_id))
        if g.start_time_et is not None and (g.game_date not in tips or g.start_time_et < tips[g.game_date]):
            tips[g.game_date] = g.start_time_et
    out: list[LaneDay] = []
    for period in range(start_period, end_period + 1):
        day = base_date + timedelta(days=period - base_period)
        tip = tips.get(day)
        out.append(LaneDay(period, day, frozenset(normalize_nba_abbrev(t) for t in teams.get(day, ())),
                           et_at(day, tip) if tip else None))
    return out


def _pending_pickups(team_id: int) -> list[PendingPickup]:
    Pickup = _pickup_model()
    rows = Pickup.select().where((Pickup.team == team_id) & (Pickup.status == PENDING))
    return [PendingPickup(r.id, r.scoring_period_id, r.nba_date, r.add_player_id, r.drop_player_id, r.lane_id)
            for r in rows]


def _open_lanes(team_id: int) -> list:
    Lane, _ = _models()
    return list(Lane.select().where((Lane.team == team_id) & (Lane.status.in_((ACTIVE, PAUSED))))
                .order_by(Lane.created_at, Lane.id))


def _insert_lane(lane_fields: dict, event_fields: dict):
    """The new lane and its first event; refused when its spot already has an open lane, or the
    team has `MAX_LANES`."""
    Lane, Event = _models()
    try:
        with db.atomic():
            open_ = Lane.select().where((Lane.team == lane_fields["team"]) & (Lane.status.in_((ACTIVE, PAUSED))))
            if open_.count() >= MAX_LANES:
                raise StreamLaneLimit()
            lane = Lane.create(**lane_fields)
            Event.create(lane=lane.id, **event_fields)
            return lane
    except IntegrityError as exc:
        raise StreamLaneExists() from exc


def _set_projections(projections: Sequence[tuple[int, int, dict]], now: datetime) -> None:
    """Other lanes' projections after a lane joined their plan: each only while it is active and
    no decision has claimed it since it was read (a decision writes its own)."""
    Lane, _ = _models()
    for lane_id, attempts, projection in projections:
        Lane.update(projection=projection, updated_at=now).where(_held(Lane, lane_id, attempts)).execute()


def _claim_due_lanes(now: datetime, limit: int) -> list:
    """Lease the due decisions of up to `limit` lanes' teams to this run (soonest first), every due
    lane of a team at once: bump attempts, push the due time by the lease, mark the decision
    running. A concurrent run sees nothing until it ends."""
    Lane, _ = _models()
    due = (Lane.status == ACTIVE) & Lane.next_decision_at.is_null(False) & (Lane.next_decision_at <= now)
    first = list(Lane.select(Lane.id, Lane.team).where(due).order_by(Lane.next_decision_at, Lane.id).limit(limit))
    if not first:
        return []
    teams = {r.team_id for r in first}
    rows = list(Lane.update(next_decision_at=now + LEASE, attempts=Lane.attempts + 1, reason=IN_PROGRESS,
                            updated_at=now)
                .where(due & Lane.team.in_(teams)).returning(Lane).execute())
    return sorted(rows, key=lambda r: (r.next_decision_at, r.id))


def _release_due_lanes(now: datetime, until: datetime) -> int:
    """Roster writes are off: push every due decision back without claiming it."""
    Lane, _ = _models()
    due = (Lane.status == ACTIVE) & Lane.next_decision_at.is_null(False) & (Lane.next_decision_at <= now)
    return Lane.update(next_decision_at=until, reason="writes_disabled", updated_at=now).where(due).execute()


def _held(Lane, lane_id: int, attempts: int):
    """Still the claim's: active (no pause or stop landed) and not claimed again since."""
    return (Lane.id == lane_id) & (Lane.status == ACTIVE) & (Lane.attempts == attempts)


@dataclass(frozen=True)
class Recorded:
    pickup_id: Optional[int] = None
    duplicate: bool = False          # the switch's pickup was already scheduled: recorded as blocked


def _record_decision(lane_id: int, attempts: int, now: datetime, lane_fields: dict, events: list[dict],
                     pickup: Optional[dict]) -> Optional[Recorded]:
    """A decision's writes, together: the lane's next step, the events, and the switch's pickup.
    None when the lane is no longer the claim's (nothing written)."""
    Lane, Event = _models()
    Pickup = _pickup_model()
    with db.atomic():
        if not Lane.update(**lane_fields, updated_at=now).where(_held(Lane, lane_id, attempts)).execute():
            return None
        pickup_id, duplicate = None, False
        if pickup is not None:
            try:
                with db.atomic():
                    pickup_id = Pickup.create(**pickup).id
            except IntegrityError:
                duplicate = True
        for event in events:
            if duplicate and event.get("action") == "switch":
                event = {**event, "action": "blocked", "reason": "already_scheduled",
                         "detail": "A pickup for that player was already scheduled"}
            Event.create(lane=lane_id, pickup_id=pickup_id if event.get("action") == "switch" else None,
                         created_at=now, **event)
        return Recorded(pickup_id, duplicate)


def _lane_step(lane_id: int, attempts: Optional[int], now: datetime, lane_fields: dict,
               event: Optional[dict] = None) -> bool:
    """A lane update (and its event) outside a decision: fenced on the claim when `attempts` is
    given. Otherwise it is the user's (pause, resume, stop, notify), on an open lane, and it bumps
    `attempts` so that a decision already running can no longer write. False when nothing changed."""
    Lane, Event = _models()
    if attempts is not None:
        where = _held(Lane, lane_id, attempts)
    else:
        where = (Lane.id == lane_id) & (Lane.status.in_((ACTIVE, PAUSED)))
        lane_fields = {**lane_fields, "attempts": Lane.attempts + 1}
    with db.atomic():
        if not Lane.update(**lane_fields, updated_at=now).where(where).execute():
            return False
        if event is not None:
            Event.create(lane=lane_id, created_at=now, **event)
        return True


def _lane_has_pending(lane_id: int) -> bool:
    Pickup = _pickup_model()
    return Pickup.select().where((Pickup.lane_id == lane_id) & (Pickup.status == PENDING)).exists()


def _adopt_executed(lane_id: int, holder_id: int, on_board: set[int]):
    """The lane's executed pickup that replaced `holder_id`, when its player is on the board: the
    settle that should have moved the holder did not (the worker died between the two)."""
    Pickup = _pickup_model()
    row = (Pickup.select().where((Pickup.lane_id == lane_id) & (Pickup.status == EXECUTED)
                                 & (Pickup.drop_player_id == holder_id))
           .order_by(Pickup.executed_at.desc()).first())
    return row if row is not None and row.add_player_id in on_board else None


def _lane_pickups(lane_id: int) -> list:
    Pickup = _pickup_model()
    return list(Pickup.select().where((Pickup.lane_id == lane_id) & (Pickup.status == PENDING)))


def _list_lanes(team_id: int, since: datetime):
    Lane, Event = _models()
    Pickup = _pickup_model()
    lanes = list(Lane.select().where((Lane.team == team_id)
                                     & ((Lane.status.in_((ACTIVE, PAUSED))) | (Lane.ended_at >= since)))
                 .order_by(Lane.created_at.desc()))
    events: dict[int, list] = {lane.id: [] for lane in lanes}
    for e in (Event.select().where(Event.lane.in_(list(events))).order_by(Event.id.desc())) if lanes else []:
        if len(events[e.lane_id]) < EVENTS_SHOWN:
            events[e.lane_id].append(e)
    pickup_ids = [e.pickup_id for es in events.values() for e in es if e.pickup_id]
    statuses = ({p.id: p.status for p in Pickup.select(Pickup.id, Pickup.status).where(Pickup.id.in_(pickup_ids))}
                if pickup_ids else {})
    return lanes, events, statuses


def _team_lane(team_id: int, lane_id: int):
    Lane, _ = _models()
    return Lane.get_or_none((Lane.id == lane_id) & (Lane.team == team_id))


def _note_pickup_settled(lane_id: int, pickup_id: int, status: str, reason: Optional[str], detail: Optional[str],
                         add: tuple[int, str, Optional[str]], drop: tuple[Optional[int], Optional[str]],
                         period: int, nba_date: date, now: datetime) -> Optional[dict]:
    """A lane pickup settled: executed makes the added player the lane's holder; anything else is a
    day the lane could not act. Returns what the email needs, or None when the lane is gone."""
    Lane, Event = _models()
    lane = Lane.get_or_none(Lane.id == lane_id)
    if lane is None:
        return None
    switch = Event.get_or_none((Event.lane == lane_id) & (Event.pickup_id == pickup_id) & (Event.action == "switch"))
    with db.atomic():
        if status == EXECUTED:
            Lane.update(holder_player_id=add[0], holder_name=add[1], holder_team=add[2], updated_at=now) \
                .where(Lane.id == lane_id).execute()
        else:
            Event.create(lane=lane_id, scoring_period_id=period, nba_date=nba_date, action="blocked",
                         add_player_id=add[0], add_name=add[1], drop_player_id=drop[0], drop_name=drop[1],
                         reason=reason, detail=detail, pickup_id=pickup_id, created_at=now)
    return {
        "lane_id": lane_id, "notify": lane.notify,
        "projected": ((switch.projected_player_id, switch.projected_name)
                      if switch is not None and switch.projected_player_id else None),
        "why": switch.reason if switch is not None else None,
        # The matchup's last day: the lane ends with this pickup, and its email says so. A lane the
        # user stopped cancels its pickups, so a stop never reaches here as a last day.
        "last_day": period >= lane.end_period,
    }


# ------------------------------- shapes ------------------------------- #


def _lane_player(player_id: int, name: Optional[str], team: Optional[str] = None,
                 nba_ids: Optional[Mapping[int, int]] = None) -> LanePlayer:
    return LanePlayer(player_id=player_id, name=name or str(player_id), team=team,
                      nba_player_id=(nba_ids or {}).get(player_id))


def _maybe_player(player_id: Optional[int], name: Optional[str], nba_ids=None) -> Optional[LanePlayer]:
    return _lane_player(player_id, name, None, nba_ids) if player_id is not None else None


def event_schema(e, statuses: Mapping[int, str], nba_ids=None) -> StreamLaneEvent:
    return StreamLaneEvent(
        id=e.id, scoring_period_id=e.scoring_period_id, nba_date=e.nba_date, action=e.action,
        add=_maybe_player(e.add_player_id, e.add_name, nba_ids),
        drop=_maybe_player(e.drop_player_id, e.drop_name, nba_ids),
        projected=_maybe_player(e.projected_player_id, e.projected_name, nba_ids),
        reason=e.reason, detail=e.detail, pickup_id=e.pickup_id,
        pickup_status=statuses.get(e.pickup_id) if e.pickup_id else None, created_at=e.created_at,
    )


def lane_schema(lane, events: Sequence = (), statuses: Optional[Mapping[int, str]] = None,
                nba_ids: Optional[Mapping[int, int]] = None) -> StreamLane:
    open_lane = lane.status in (ACTIVE, PAUSED)
    return StreamLane(
        id=lane.id, team_id=lane.team_id, status=lane.status, notify=lane.notify,
        holder=_lane_player(lane.holder_player_id, lane.holder_name, lane.holder_team, nba_ids),
        original=_lane_player(lane.original_player_id, lane.original_name, None, nba_ids),
        start_period=lane.start_period, end_period=lane.end_period, end_date=lane.end_date,
        next_period=lane.next_period if open_lane else None,
        next_decision_at=lane.next_decision_at if lane.status == ACTIVE else None,
        reason=None if lane.reason == IN_PROGRESS else lane.reason, detail=lane.detail,
        projection=LaneProjection.model_validate(lane.projection) if lane.projection else None,
        events=[event_schema(e, statuses or {}, nba_ids) for e in events],
        created_at=lane.created_at, ended_at=lane.ended_at,
    )


def _notice(lane, action: str, reason: str, *, team_name: str = "", detail: Optional[str] = None,
            period: Optional[int] = None, nba_date: Optional[date] = None) -> LaneNotice:
    return LaneNotice(
        lane_id=lane.id, team_id=lane.team_id, user_id=lane.user_id, team_name=team_name, notify=lane.notify,
        action=action, reason=reason, detail=detail, scoring_period_id=period, nba_date=nba_date,
        holder=ScheduledPickupPlayer(player_id=lane.holder_player_id, name=lane.holder_name, team=lane.holder_team),
    )


# ------------------------------- service ------------------------------- #


@dataclass
class Prepared:
    days: list[LaneDay]
    plan: MultiStreamPlan
    projections: list[LaneProjection]               # one per spot, in the holders' order
    players: dict[int, LanePlayer] = field(default_factory=dict)


@dataclass
class Deciding:
    """A claimed lane through its run: the day it decides, its holder, and what it has to record
    whatever it decides (missed days)."""
    lane: Any
    period: int
    holder: LineupPlayer
    holder_fields: dict
    events: list[dict]
    notice: Optional[LaneNotice]


class StreamLaneService:

    # ---- shared ----

    @staticmethod
    async def _pool(league_info: LeagueInfo, adapter, team_id: int) -> list[PoolPlayer]:
        """The free agents the lane may take: the pool as the market reads it, out players left
        out, a waiver claim available the day after its waivers clear."""
        resp = await adapter.get_free_agents(league_info, FA_COUNT, team_id=team_id)
        if resp.status != ApiStatus.SUCCESS:
            raise RuntimeError(resp.message or "could not read the free-agent pool")
        out: list[PoolPlayer] = []
        for fa in resp.data or []:
            if _is_out(fa.injury_status):
                continue
            available_on = None
            if fa.acquisition_status == "waivers":
                if fa.waivers_until is None:
                    continue
                available_on = fa.waivers_until + timedelta(days=1)
            out.append(PoolPlayer(fa.player_id, fa.name, normalize_nba_abbrev(fa.team), active_slots(fa.valid_positions),
                                  float(fa.avg_points or 0.0), fa.default_position_id, available_on))
        return out

    @staticmethod
    def _board_today(board: LineupState) -> tuple[int, date]:
        if board.scoring_period_id is None or not board.nba_date:
            raise _invalid("no_scoring_period", "ESPN reports no current day for this league")
        return board.scoring_period_id, date.fromisoformat(board.nba_date)

    @staticmethod
    def _end_period(board: LineupState) -> tuple[int, date]:
        """The current matchup's last ESPN day."""
        period, today = StreamLaneService._board_today(board)
        acq = board.acquisitions
        if acq is None or acq.matchup_end is None:
            raise _invalid("no_matchup_end", "ESPN didn't say when this matchup ends")
        return period + (acq.matchup_end - today).days, acq.matchup_end

    @staticmethod
    async def _prepare(team_id: int, league_info: LeagueInfo, adapter, board: LineupState,
                       holders: Sequence[LineupPlayer], start_period: int, end_period: int, rehold_after: int,
                       now: datetime, *, lane_ids: Iterable[int] = ()) -> Prepared:
        """The plan for the spots `holders` hold, together, from `start_period`: the lanes `lane_ids`
        decide it, so their own pending pickups (if any) are not taken as given."""
        base_period, base_date = StreamLaneService._board_today(board)
        days = await run_db("lane.days", _days, start_period, end_period, base_period, base_date)
        deciding = set(lane_ids)
        pending = [p for p in await run_db("lane.pending", _pending_pickups, team_id) if p.lane_id not in deciding]
        pool = await StreamLaneService._pool(league_info, adapter, team_id)
        acq = board.acquisitions
        first_day_adds = None
        if acq is not None and acq.per == "day" and acq.limit is not None and board.espn_team_id is not None:
            try:
                first_day_adds = await adds_for_day(league_info, board.espn_team_id, start_period)
            except Exception as exc:  # ESPN refuses an over-limit add itself
                log.warning("stream_lane_day_adds_unavailable", team_id=team_id, error=str(exc))
        budget = lane_budget(acq, days, pending, first_day_adds=first_day_adds)
        plan = await run_cpu("lane.plan", plan_lanes, board, [h.player_id for h in holders], pool, days, pending,
                             budget, rehold_after)
        held = {pid for spot in plan.spots for pid in spot} | set(plan.holder_ids)
        nba_ids = await run_db("lane.nba_ids", nba_ids_by_espn_id, sorted(held))
        players = {p.player_id: _lane_player(p.player_id, p.name, p.team, nba_ids) for p in board.players
                   if p.player_id in held}
        players.update({f.player_id: _lane_player(f.player_id, f.name, f.team, nba_ids) for f in pool
                        if f.player_id in held and f.player_id not in players})
        projections = projections_of(plan, days, players, adds_left=budget.adds, rehold_after=rehold_after, now=now)
        if not plan.optimal:
            log.warning("stream_lanes_plan_unproven", team_id=team_id, lanes=len(holders), start=start_period)
        return Prepared(days, plan, projections, players)

    @staticmethod
    def _check_room(open_lanes: Sequence, player_id: int) -> None:
        """A new lane needs a spot no open lane streams, and room under `MAX_LANES`."""
        if any(lane.holder_player_id == player_id for lane in open_lanes):
            raise StreamLaneExists()
        if len(open_lanes) >= MAX_LANES:
            raise StreamLaneLimit()

    @staticmethod
    async def _together(team_id: int, board: LineupState, start_period: int, end_period: int,
                        open_lanes: Sequence) -> list:
        """The open lanes a lane started now decides its first day with: active, deciding that day
        too, their holder on the board, and no pickup of theirs pending (such a lane decides once it
        settles, and its pickup is part of the roster meanwhile)."""
        on_board = {p.player_id for p in board.players}
        waiting = {p.lane_id for p in await run_db("lane.pending", _pending_pickups, team_id) if p.lane_id}
        return [lane for lane in open_lanes
                if lane.status == ACTIVE and lane.next_period <= start_period and lane.end_period == end_period
                and lane.holder_player_id in on_board and lane.id not in waiting]

    @staticmethod
    async def _setup(team, league_info: LeagueInfo, player_id: int, now: datetime):
        """The board, the spot's player, and the horizon a lane started now would plan."""
        adapter = get_provider_adapter(league_info.provider)
        if not adapter.capabilities(league_info).transactions:
            raise RosterWriteBlocked(message=unavailable_message("roster_changes", league_info.provider),
                                     data={"reason": "provider_not_supported"})
        slots = _roster_slots(team)
        board = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots)
        holder = next((p for p in board.players if p.player_id == player_id), None)
        if holder is None:
            raise _invalid("not_on_roster", "That player is not on your roster")
        if holder.lineup_slot_id == IR_SLOT_ID:
            raise _invalid("on_ir", f"{holder.name} is on IR — a lane streams an active spot")
        period, _ = StreamLaneService._board_today(board)
        end_period, end_date = StreamLaneService._end_period(board)
        today = (await run_db("lane.days", _days, period, period, period, date.fromisoformat(board.nba_date)))[0]
        start_period = first_open_period(today, now)
        if start_period > end_period:
            raise _invalid("matchup_over", "The matchup has no day left a pickup could count for")
        rehold_after = rehold_after_for(board.acquisitions)
        return adapter, board, holder, start_period, end_period, end_date, rehold_after

    # ---- the user's routes ----

    @staticmethod
    async def _plan_with(team, league_info: LeagueInfo, adapter, board: LineupState, holder: LineupPlayer,
                         start: int, end: int, rehold: int, now: datetime, open_lanes: Sequence):
        """A lane on `holder`'s spot planned with the lanes it would decide its first day with: the
        plan, and those lanes."""
        together = await StreamLaneService._together(team.team_id, board, start, end, open_lanes)
        on_board = {p.player_id: p for p in board.players}
        holders = [*(on_board[lane.holder_player_id] for lane in together), holder]
        prepared = await StreamLaneService._prepare(team.team_id, league_info, adapter, board, holders, start, end,
                                                    rehold, now, lane_ids=[lane.id for lane in together])
        return prepared, together

    @staticmethod
    async def plan(team, league_info: LeagueInfo, req: StreamLanePlanReq) -> StreamLanePlanResp:
        now = datetime.now(timezone.utc)
        open_lanes = await run_db("lane.open", _open_lanes, team.team_id)
        StreamLaneService._check_room(open_lanes, req.player_id)
        adapter, board, holder, start, end, end_date, rehold = await StreamLaneService._setup(
            team, league_info, req.player_id, now)
        prepared, together = await StreamLaneService._plan_with(team, league_info, adapter, board, holder, start, end,
                                                                rehold, now, open_lanes)
        projection = prepared.projections[-1]
        window = await run_db("lane.window", _window_for, prepared.days[0].nba_date)
        others = [StreamLanePlanOther(lane_id=lane.id, holder=prepared.players[lane.holder_player_id], projection=p)
                  for lane, p in zip(together, prepared.projections)]
        return StreamLanePlanResp(
            status=ApiStatus.SUCCESS,
            message=f"Projected gain {projection.gain:+.1f} with {projection.adds} add(s)",
            data=StreamLanePlan(holder=prepared.players[holder.player_id], start_period=start, end_period=end,
                                end_date=end_date, first_decision_at=max(window.not_before_at, now),
                                projection=projection, others=others),
        )

    @staticmethod
    async def start(team, league_info: LeagueInfo, req: StreamLaneStartReq) -> StreamLaneResp:
        if not settings.roster_writes_enabled:
            raise RosterWriteDisabled()
        now = datetime.now(timezone.utc)
        open_lanes = await run_db("lane.open", _open_lanes, team.team_id)
        StreamLaneService._check_room(open_lanes, req.player_id)
        adapter, board, holder, start, end, end_date, rehold = await StreamLaneService._setup(
            team, league_info, req.player_id, now)
        if not board.can_write:
            raise RosterWriteBlocked(data={"reason": board.write_blocked_reason})
        pending = await run_db("lane.pending", _pending_pickups, team.team_id)
        if any(p.drop_player_id == holder.player_id for p in pending):
            raise _invalid("scheduled_out", f"A pickup already scheduled drops {holder.name} — cancel it first")
        prepared, together = await StreamLaneService._plan_with(team, league_info, adapter, board, holder, start, end,
                                                                rehold, now, open_lanes)
        projection = prepared.projections[-1]
        window = await run_db("lane.window", _window_for, prepared.days[0].nba_date)
        lane = await run_db("lane.insert", _insert_lane, dict(
            user=team.user_id, team=team.team_id, holder_player_id=holder.player_id, holder_name=holder.name,
            holder_team=holder.team, original_player_id=holder.player_id, original_name=holder.name,
            start_period=start, end_period=end, end_date=end_date, status=ACTIVE, notify=req.notify,
            next_period=start, next_decision_at=window.not_before_at, attempts=0,
            projection=projection.model_dump(mode="json"), created_at=now, updated_at=now,
        ), dict(scoring_period_id=start, nba_date=prepared.days[0].nba_date, action="started", created_at=now))
        if together:
            # The lanes it decides with share their adds and seats with it now: their projections change too.
            await run_db("lane.projections", _set_projections,
                         [(other.id, other.attempts, p.model_dump(mode="json"))
                          for other, p in zip(together, prepared.projections)], now)
        log.info("stream_lane_started", team_id=team.team_id, lane_id=lane.id, holder=holder.player_id,
                 start=start, end=end, gain=projection.gain, adds=projection.adds, together=len(together),
                 first_decision_at=window.not_before_at.isoformat())
        return StreamLaneResp(status=ApiStatus.SUCCESS,
                              message=f"Streaming {holder.name}'s spot through {end_date:%a %b %-d}",
                              data=lane_schema(lane))

    @staticmethod
    async def list_for_team(team) -> StreamLaneListResp:
        since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
        lanes, events, statuses = await run_db("lane.list", _list_lanes, team.team_id, since)
        ids = {lane.holder_player_id for lane in lanes} | {lane.original_player_id for lane in lanes}
        ids |= {pid for es in events.values() for e in es
                for pid in (e.add_player_id, e.drop_player_id, e.projected_player_id) if pid}
        nba_ids = await run_db("lane.nba_ids", nba_ids_by_espn_id, sorted(ids)) if ids else {}
        shaped = [lane_schema(lane, events[lane.id], statuses, nba_ids) for lane in lanes]
        open_ = sorted((s for s in shaped if s.status in (ACTIVE, PAUSED)), key=lambda s: (s.created_at, s.id))
        recent = sorted((s for s in shaped if s.status == ENDED), key=lambda s: s.ended_at or s.created_at, reverse=True)
        return StreamLaneListResp(status=ApiStatus.SUCCESS, message=f"{len(open_)} open lane(s)",
                                  data=StreamLaneListData(open=open_, recent=recent))

    @staticmethod
    async def update(team, league_info: LeagueInfo, lane_id: int, req: StreamLaneUpdateReq) -> StreamLaneResp:
        lane = await run_db("lane.get", _team_lane, team.team_id, lane_id)
        if lane is None:
            raise NotFoundError("STREAM_LANE_NOT_FOUND", "Stream lane not found")
        if lane.status == ENDED:
            raise StreamLaneEnded()
        now = datetime.now(timezone.utc)
        changes: dict[str, Any] = {}
        event = None
        message = "Stream lane updated"
        if req.notify is not None and req.notify != lane.notify:
            changes["notify"] = req.notify
        if req.status == PAUSED and lane.status == ACTIVE:
            # Nothing more changes on ESPN while it is paused: the pickup the lane scheduled goes (unless it
            # is being made right now), and its day is decided again on resume.
            cancelled = await StreamLaneService._cancel_pickups(team, lane, now)
            nxt = min([lane.next_period, *cancelled])
            changes.update(status=PAUSED, next_period=nxt, next_decision_at=None, reason="paused_by_user", detail=None)
            event = dict(action="paused", reason="paused_by_user", scoring_period_id=nxt)
            message = "Stream lane paused"
        elif req.status == ACTIVE and lane.status == PAUSED:
            resumed = await StreamLaneService._resume_fields(team, league_info, lane, now)
            if resumed is None:      # the matchup has no day left: the lane ends instead
                changes.update(status=ENDED, next_decision_at=None, reason="matchup_over", ended_at=now)
                event = dict(action="ended", reason="matchup_over")
                message = "The matchup has no day left a pickup could count for — the lane has ended"
            else:
                changes.update(resumed)
                event = dict(action="resumed", scoring_period_id=resumed["next_period"])
                message = "Stream lane resumed"
        if changes:
            if not await run_db("lane.update", _lane_step, lane.id, None, now, changes, event):
                raise StreamLaneEnded()
            if changes.get("status") == PAUSED:
                # As for a stop: a decision that landed after the loop above may have scheduled one since.
                await StreamLaneService._cancel_pickups(team, lane, now, busy=False)
            log.info("stream_lane_updated", team_id=team.team_id, lane_id=lane.id, changes=sorted(changes))
        lane = await run_db("lane.get", _team_lane, team.team_id, lane_id)
        return StreamLaneResp(status=ApiStatus.SUCCESS, message=message, data=lane_schema(lane))

    @staticmethod
    async def _resume_fields(team, league_info: LeagueInfo, lane, now: datetime) -> Optional[dict]:
        """The lane's next decision from now, or None when the matchup has no day left. The holder
        must still be on the roster: a lane resumes on its spot, not on a new one — and, as at the
        start, no pickup the user scheduled while it was paused may drop them."""
        adapter = get_provider_adapter(league_info.provider)
        board = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
        holder = next((p for p in board.players if p.player_id == lane.holder_player_id), None)
        if holder is None:
            raise _invalid("holder_gone", f"{lane.holder_name} is no longer on your roster — start a new lane "
                                          "from a seat instead")
        pending = await run_db("lane.pending", _pending_pickups, team.team_id)
        if any(p.lane_id != lane.id and p.drop_player_id == holder.player_id for p in pending):
            raise _invalid("scheduled_out", f"A pickup already scheduled drops {holder.name} — cancel it first")
        period, today_date = StreamLaneService._board_today(board)
        today = (await run_db("lane.days", _days, period, period, period, today_date))[0]
        nxt = max(lane.next_period, first_open_period(today, now))
        if nxt > lane.end_period:
            return None
        window = await run_db("lane.window", _window_for, today_date + timedelta(days=nxt - period))
        return dict(status=ACTIVE, next_period=nxt, next_decision_at=window.not_before_at, reason=None, detail=None)

    @staticmethod
    async def stop(team, lane_id: int) -> StreamLaneResp:
        lane = await run_db("lane.get", _team_lane, team.team_id, lane_id)
        if lane is None:
            raise NotFoundError("STREAM_LANE_NOT_FOUND", "Stream lane not found")
        if lane.status == ENDED:
            raise StreamLaneEnded()
        now = datetime.now(timezone.utc)
        # A pickup the lane scheduled goes with it — unless it is being made right now.
        await StreamLaneService._cancel_pickups(team, lane, now)
        if not await run_db("lane.stop", _lane_step, lane.id, None, now,
                            dict(status=ENDED, next_decision_at=None, reason="stopped", detail=None, ended_at=now),
                            dict(action="ended", reason="stopped")):
            raise StreamLaneEnded()
        # A decision that was mid-flight is fenced out by the stop; one that landed first may have
        # scheduled a pickup since the loop above.
        await StreamLaneService._cancel_pickups(team, lane, now, busy=False)
        log.info("stream_lane_stopped", team_id=team.team_id, lane_id=lane.id)
        lane = await run_db("lane.get", _team_lane, team.team_id, lane_id)
        return StreamLaneResp(status=ApiStatus.SUCCESS, message="Stream lane stopped", data=lane_schema(lane))

    @staticmethod
    async def _cancel_pickups(team, lane, now: datetime, *, busy: bool = True) -> list[int]:
        """Cancel the lane's pending pickups; the ESPN days of those cancelled. With `busy`, one being
        made right now refuses (StreamLaneBusy): the user's pause or stop must not land under it."""
        cancelled: list[int] = []
        for pickup in await run_db("lane.pickups", _lane_pickups, lane.id):
            found = await run_db("pickup.cancel", _cancel_pickup, team.team_id, pickup.id, now)
            if found is not None and found[0] == IN_PROGRESS and busy:
                raise StreamLaneBusy()
            if found is not None and found[0] == PENDING:
                cancelled.append(pickup.scoring_period_id)
        return cancelled

    # ---- the executor's tick ----

    @staticmethod
    async def release_due(now: datetime, until: datetime) -> int:
        return await run_db("lane.release", _release_due_lanes, now, until)

    @staticmethod
    async def decide_due(now: datetime, limit: int) -> list[LaneNotice]:
        """Make every due decision, for up to `limit` lanes' teams, each team's at once. Returns what
        the users hear about: a lane that paused, a day it missed. A switch is told by its pickup's
        result."""
        lanes = await run_db("lane.claim", _claim_due_lanes, now, limit)
        by_team: dict[int, list] = {}
        for lane in lanes:
            by_team.setdefault(lane.team_id, []).append(lane)
        notices: list[LaneNotice] = []
        for team_lanes in by_team.values():
            try:
                notices.extend(await StreamLaneService._decide_team(team_lanes, now))
            except Exception as exc:  # one broken team must not take the tick down
                log.exception("stream_lane_error", lane_ids=[lane.id for lane in team_lanes],
                              team_id=team_lanes[0].team_id, error=str(exc))
                # Retry soon; a lane this run already paused or ended is fenced out of the write.
                for lane in team_lanes:
                    await StreamLaneService._wait(lane, now, "error", detail=str(exc)[:300])
        if lanes:
            log.info("stream_lanes_run", due=len(lanes), teams=len(by_team), notices=len(notices))
        return notices

    @staticmethod
    async def _decide_team(lanes: Sequence, now: datetime) -> list[LaneNotice]:
        """One team's claimed lanes: what holds each back (its pickup pending, its holder gone, the
        matchup over) is settled lane by lane; the rest decide their day together."""
        notices: list[LaneNotice] = []

        def tell(notice: Optional[LaneNotice]) -> None:
            if notice is not None:
                notices.append(notice)

        team_id, user_id = lanes[0].team_id, lanes[0].user_id
        try:
            league_info, slots = await run_db("lane.job_team", _load_team_for_job, team_id, user_id)
        except NotFoundError:
            for lane in lanes:
                tell(await StreamLaneService._end(lane, now, "team_not_found"))
            return notices
        team_name = league_info.team_name or ""
        adapter = get_provider_adapter(league_info.provider)
        if not adapter.capabilities(league_info).transactions:
            for lane in lanes:
                tell(await StreamLaneService._pause(lane, now, "provider_not_supported", team_name=team_name))
            return notices
        live = []
        for lane in lanes:
            if await run_db("lane.has_pending", _lane_has_pending, lane.id):
                # Its last pickup hasn't settled: who holds the spot is not known yet.
                await StreamLaneService._wait(lane, now, "pickup_pending", after=PICKUP_WAIT)
            elif lane.next_period > lane.end_period:
                tell(await StreamLaneService._end(lane, now, "matchup_over"))
            else:
                live.append(lane)
        if not live:
            return notices

        try:
            board = await adapter.read_lineup(team_id, league_info, fallback_slot_counts=slots)
        except ProviderAuthError:
            for lane in live:
                tell(await StreamLaneService._pause(lane, now, "auth_expired", team_name=team_name))
            return notices
        except Exception as exc:
            for lane in live:
                await StreamLaneService._wait(lane, now, "provider_error", detail=str(exc)[:300])
            return notices
        if not board.can_write:
            reason = f"can_write:{board.write_blocked_reason}"
            for lane in live:
                if board.write_blocked_reason in ("no_scoring_period", "writes_disabled"):
                    await StreamLaneService._wait(lane, now, reason)
                else:
                    tell(await StreamLaneService._pause(lane, now, reason, team_name=team_name))
            return notices
        if board.scoring_period_id is None or not board.nba_date:
            for lane in live:
                await StreamLaneService._wait(lane, now, "no_scoring_period")
            return notices
        today_period, today_date = board.scoring_period_id, date.fromisoformat(board.nba_date)
        today = (await run_db("lane.days", _days, today_period, today_period, today_period, today_date))[0]
        open_period = first_open_period(today, now)
        on_board = {p.player_id: p for p in board.players}

        deciding: list[Deciding] = []
        for lane in live:
            events: list[dict] = []
            notice: Optional[LaneNotice] = None
            period = lane.next_period
            if period < open_period:
                # The decision came too late for those days (the tick or ESPN was down): they pass.
                last = min(open_period - 1, lane.end_period)
                detail = f"No decision was made in time for ESPN day{'s' if last > period else ''} " \
                         f"{period}{f'-{last}' if last > period else ''}"
                missed_date = today_date + timedelta(days=period - today_period)
                events.append(dict(action="blocked", reason="missed", detail=detail, scoring_period_id=period,
                                   nba_date=missed_date))
                notice = _notice(lane, "blocked", "missed", team_name=team_name, detail=detail, period=period,
                                 nba_date=missed_date)
                period = open_period
            if period > lane.end_period:
                tell(await StreamLaneService._end(lane, now, "matchup_over", events=events, notice=notice))
                continue
            holder = on_board.get(lane.holder_player_id)
            holder_fields: dict[str, Any] = {}
            if holder is None:
                adopted = await run_db("lane.adopt", _adopt_executed, lane.id, lane.holder_player_id, set(on_board))
                if adopted is None:
                    tell(await StreamLaneService._pause(lane, now, "holder_gone", team_name=team_name,
                                                        detail=f"{lane.holder_name} is no longer on the roster",
                                                        events=events))
                    continue
                holder = on_board[adopted.add_player_id]
                holder_fields = dict(holder_player_id=holder.player_id, holder_name=holder.name,
                                     holder_team=holder.team)
            deciding.append(Deciding(lane, period, holder, holder_fields, events, notice))

        # Lanes deciding the same day (to the same matchup end) are planned together.
        groups: dict[tuple[int, int], list[Deciding]] = {}
        for d in deciding:
            groups.setdefault((d.period, d.lane.end_period), []).append(d)
        rehold = rehold_after_for(board.acquisitions)
        for (period, end_period), group in sorted(groups.items()):
            try:
                notices.extend(await StreamLaneService._decide_together(
                    group, period, end_period, board, league_info, adapter, rehold, today_period, today_date,
                    team_name, now))
            except Exception as exc:  # the other groups' decisions stand
                log.exception("stream_lane_error", lane_ids=[d.lane.id for d in group], team_id=team_id,
                              error=str(exc))
                for d in group:
                    await StreamLaneService._wait(d.lane, now, "error", detail=str(exc)[:300])
        return notices

    @staticmethod
    async def _decide_together(group: Sequence[Deciding], period: int, end_period: int, board: LineupState,
                               league_info: LeagueInfo, adapter, rehold: int, today_period: int, today_date: date,
                               team_name: str, now: datetime) -> list[LaneNotice]:
        """One plan for the lanes deciding `period`, and each lane's step from it: the switch its spot
        makes that day (a scheduled pickup), or a keep."""
        team_id = group[0].lane.team_id
        notices: list[LaneNotice] = []
        day_date = today_date + timedelta(days=period - today_period)
        window = await run_db("lane.window", _window_for, day_date)
        if window.not_before_at > now:
            # Too early: a pickup for the day can't be made before its window opens (the day
            # before's first tip-off), so the decision waits to be made with the pool as it is then.
            for d in group:
                await StreamLaneService._wait(d.lane, now, "window_not_open", at=window.not_before_at)
            return notices
        try:
            prepared = await StreamLaneService._prepare(team_id, league_info, adapter, board,
                                                        [d.holder for d in group], period, end_period, rehold, now,
                                                        lane_ids=[d.lane.id for d in group])
        except ProviderAuthError:
            for d in group:
                notice = await StreamLaneService._pause(d.lane, now, "auth_expired", team_name=team_name,
                                                        events=d.events)
                if notice is not None:
                    notices.append(notice)
            return notices
        except Exception as exc:
            for d in group:
                if d.events:   # the missed days are recorded either way
                    log.warning("stream_lane_missed_unrecorded", lane_id=d.lane.id, error=str(exc))
                await StreamLaneService._wait(d.lane, now, "provider_error", detail=str(exc)[:300])
            return notices
        plan = prepared.plan
        moves = {sw.spot: sw for sw in plan.first_moves}
        day = prepared.days[0]
        # The next decision: the next day's, when a pickup for it can first be made (this day's first
        # tip-off). After the last day, the run at its first tip-off ends the lanes (once their last
        # pickups have settled).
        if period < end_period:
            next_window = await run_db("lane.window", _window_for, day.nba_date + timedelta(days=1))
            next_at = next_window.not_before_at
        else:
            next_at = window.deadline_at or rollover_at(day.nba_date + timedelta(days=1))
        for k, d in enumerate(group):
            lane, holder = d.lane, d.holder
            move = moves.get(k)
            chosen = move.add_id if move is not None else holder.player_id
            projected, why = compare_to_projection(lane.projection, period, chosen, holder.player_id,
                                                   plan.considered)
            base_event = dict(scoring_period_id=period, nba_date=day.nba_date,
                              projected_player_id=projected.player_id if projected else None,
                              projected_name=projected.name if projected else None, reason=why)
            events = list(d.events)
            pickup = None
            if move is not None:
                incoming = prepared.players[move.add_id]
                pickup = dict(user=lane.user_id, team=lane.team_id, add_player_id=incoming.player_id,
                              add_name=incoming.name, add_team=incoming.team, drop_player_id=holder.player_id,
                              drop_name=holder.name, drop_team=holder.team, scoring_period_id=period,
                              nba_date=day.nba_date, not_before_at=window.not_before_at,
                              deadline_at=window.deadline_at, status=PENDING, created_at=now, updated_at=now,
                              lane_id=lane.id)
                events.append(dict(action="switch", add_player_id=incoming.player_id, add_name=incoming.name,
                                   drop_player_id=holder.player_id, drop_name=holder.name, **base_event))
            else:
                events.append(dict(action="keep", **base_event))
            lane_fields = dict(next_period=period + 1, next_decision_at=next_at, reason=None, detail=None,
                               projection=prepared.projections[k].model_dump(mode="json"), **d.holder_fields)
            try:
                recorded = await run_db("lane.record", _record_decision, lane.id, lane.attempts, now, lane_fields,
                                        events, pickup)
            except Exception as exc:  # the others' decisions stand
                log.exception("stream_lane_record_failed", lane_id=lane.id, team_id=team_id, error=str(exc))
                await StreamLaneService._wait(lane, now, "error", detail=str(exc)[:300])
                continue
            if recorded is None:
                log.warning("stream_lane_claim_lost", lane_id=lane.id, team_id=team_id, step="decide")
                continue
            log.info("stream_lane_decided", lane_id=lane.id, team_id=team_id, period=period,
                     action="switch" if move is not None else "keep", add=move.add_id if move else None,
                     drop=holder.player_id if move else None, why=why, gain=prepared.projections[k].gain,
                     adds=prepared.projections[k].adds, lanes=len(group), team_gain=plan.gain, team_adds=plan.adds,
                     optimal=plan.optimal, pickup_id=recorded.pickup_id, duplicate=recorded.duplicate,
                     next_decision_at=next_at.isoformat())
            if d.notice is not None:
                notices.append(d.notice)
        return notices

    @staticmethod
    async def _wait(lane, now: datetime, reason: str, *, after: timedelta = RETRY, at: Optional[datetime] = None,
                    detail: Optional[str] = None) -> None:
        """Make the decision later — at `at`, or after `after` — and tell nobody (a day that passes
        meanwhile is told, as missed)."""
        when = at if at is not None else now + after
        if not await run_db("lane.wait", _lane_step, lane.id, lane.attempts, now,
                            dict(next_decision_at=when, reason=reason, detail=detail)):
            log.warning("stream_lane_claim_lost", lane_id=lane.id, team_id=lane.team_id, step="wait")
            return None
        log.info("stream_lane_waiting", lane_id=lane.id, team_id=lane.team_id, reason=reason,
                 next_decision_at=when.isoformat())
        return None

    @staticmethod
    async def _pause(lane, now: datetime, reason: str, *, team_name: str = "", detail: Optional[str] = None,
                     events: Sequence[dict] = ()) -> Optional[LaneNotice]:
        """The lane can't go on by itself: it waits for the user, who is told why."""
        recorded = await run_db("lane.pause", _record_decision, lane.id, lane.attempts, now,
                                dict(status=PAUSED, next_decision_at=None, reason=reason, detail=detail),
                                [*events, dict(action="paused", reason=reason, detail=detail,
                                               scoring_period_id=lane.next_period)], None)
        if recorded is None:
            log.warning("stream_lane_claim_lost", lane_id=lane.id, team_id=lane.team_id, step="pause")
            return None
        log.info("stream_lane_paused", lane_id=lane.id, team_id=lane.team_id, reason=reason)
        return _notice(lane, "paused", reason, team_name=team_name, detail=detail)

    @staticmethod
    async def _end(lane, now: datetime, reason: str, *, events: Sequence[dict] = (),
                   notice: Optional[LaneNotice] = None) -> Optional[LaneNotice]:
        recorded = await run_db("lane.end", _record_decision, lane.id, lane.attempts, now,
                                dict(status=ENDED, next_decision_at=None, reason=reason, detail=None, ended_at=now),
                                [*events, dict(action="ended", reason=reason)], None)
        if recorded is None:
            log.warning("stream_lane_claim_lost", lane_id=lane.id, team_id=lane.team_id, step="end")
            return None
        log.info("stream_lane_ended", lane_id=lane.id, team_id=lane.team_id, reason=reason)
        # A notice that comes with the end (days missed up to the matchup's last) says the lane is over.
        return notice.model_copy(update={"ended": True}) if notice is not None else None

    # ---- the pickup executor's settle ----

    @staticmethod
    async def note_pickup_settled(row, status: str, reason: Optional[str], detail: Optional[str],
                                  now: datetime) -> Optional[PickupLane]:
        """What a lane pickup's settle means for the lane (see `_note_pickup_settled`), and what its
        email needs. Never raises: the pickup has settled whatever happens here."""
        try:
            noted = await run_db("lane.pickup_settled", _note_pickup_settled, row.lane_id, row.id, status, reason,
                                 detail, (row.add_player_id, row.add_name, row.add_team),
                                 (row.drop_player_id, row.drop_name), row.scoring_period_id, row.nba_date, now)
        except Exception as exc:
            log.exception("stream_lane_pickup_note_failed", lane_id=row.lane_id, pickup_id=row.id, error=str(exc))
            return None
        if noted is None:
            return None
        projected = noted["projected"]
        return PickupLane(
            lane_id=noted["lane_id"], notify=noted["notify"], why=noted["why"],
            projected=ScheduledPickupPlayer(player_id=projected[0], name=projected[1]) if projected else None,
            last_day=noted.get("last_day", False),
        )
