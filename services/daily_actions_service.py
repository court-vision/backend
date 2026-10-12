"""
Daily actions: today's recommended roster moves for a saved team, in one read.

    GET /teams/{id}/actions  ->  DailyActionsService.read

One ESPN board read (`LineupReadService.read`), the fill-only plan (`plan_fill`),
IR housekeeping (`plan_ir`) and a daily streamer swap over the free-agent pool
(`StreamerService.find_streamers`, daily mode) become one ordered list of rows,
each carrying exactly what the client stages: the lineup moves, or the pickup and
the player to drop. Every row is judged against the CURRENT board so each can be
staged on its own; the client re-reads after any write. Never writes.

The pickup row is a streaming move, not an upgrade engine: the player to drop is
the lowest-value roster player with no game today, the pickup is the day's best
free agent, and the value comparison only checks that the two are comparable.
Two clocks say "today" — ESPN's scoring period for the board, the 2 AM ET rule
for the streamer pool — so the swap is offered only when both agree on the date.

A drop candidate's value is the board's recent average only when that average
rests on a real sample; a player with no games behind it (back from a lost
season, a rookie before his debut) or only a couple is weighed at his season
projection instead (`drop_value`), and one we know nothing about is never the drop.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, NamedTuple, Optional, Sequence

from core.logging import get_logger
from db.base import run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.daily_actions import (
    DailyAction,
    DailyActionPlayer,
    DailyActionTransaction,
    DailyActionsData,
    DailyActionsResp,
)
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.streamer import StreamerData, StreamerMode, StreamerPlayerResp
from services import schedule_service
from services.espn_service import EspnService
from services.lineup_editor_service import (
    _clock,
    _roster_slots,
    _slot_name,
    move_results,
    planner_players,
    slot_counts_of,
    unfilled_results,
)
from services.lineup_planner import IR_SLOT_ID, IrAction, Move, Plan, plan_fill, plan_ir
from services.lineup_read_service import LineupReadService, _games_on  # noqa: F401  (tests stand in for its read here)
from services.matchup_days import index_games
from services.player_value_service import PlayerValueService
from services.providers import get_provider_adapter, unavailable_message
from services.streamer_service import StreamerService

log = get_logger("daily_actions")
# The drop candidate is "streamable" only when the day's pool holds a comparable player.
STREAMABLE_RATIO = 0.85
DEFAULT_FA_COUNT = 100
# A recent average on fewer games than this is no reason to drop a player.
MIN_SAMPLE_GAMES = 3
# The rolling window the board's values come from (`PlayerValueService.avg_points_for`'s default).
BOARD_WINDOW_DAYS = 14
KIND_ORDER = {"ir_out": 0, "ir_in": 1, "start": 2, "add_drop": 3, "add": 3}
ROSTER_FULL_DETAIL = "Roster full — drop a player first"


@dataclass(frozen=True)
class DropEvidence:
    """What stands behind the drop candidates' board values, by ESPN id."""
    games: Mapping[int, int]              # games in the board's rolling window (absent = none)
    projected: Mapping[int, float]        # season projection, on the board's value scale


class DropValue(NamedTuple):
    value: float
    projected: bool                       # the season projection, not the board's average


@dataclass(frozen=True)
class TransactionSuggestion:
    pickup: StreamerPlayerResp
    drop: Optional[LineupPlayer]          # None when a roster seat is open
    drop_value: Optional[DropValue] = None


# ------------------------------- pure ------------------------------- #


def chains_of(moves: Sequence[Move]) -> list[list[Move]]:
    """The flat fill plan as [start, shifts..., bench?] chains — exact, because
    `_apply_path` emits exactly that sequence per filler."""
    chains: list[list[Move]] = []
    for m in moves:
        if m.role == "start" or not chains:
            chains.append([m])
        else:
            chains[-1].append(m)
    return chains


def has_open_seat(state: LineupState) -> bool:
    """ESPN's roster limit as the add/drop needs it: the players off IR must number fewer
    than the non-IR seats. An empty IR seat is not a seat for a healthy player, and the
    bench absorbs an add even when its own slot count is full (ESPN parks the pickup
    there rather than in an open active slot he is not eligible for)."""
    counts = slot_counts_of(state)
    seats = sum(v for k, v in counts.items() if k != IR_SLOT_ID)
    off_ir = sum(1 for p in state.players if p.lineup_slot_id != IR_SLOT_ID)
    return off_ir < seats


def drop_candidates(state: LineupState, ir_eligible: Mapping[int, bool]) -> list[LineupPlayer]:
    """Who the swap may drop: idle today, unlocked, off IR and not headed there."""
    return [p for p in state.players
            if not p.locked and p.lineup_slot_id != IR_SLOT_ID
            and not ir_eligible.get(p.player_id, False) and not p.has_game_today]


def drop_value(p: LineupPlayer, evidence: Optional[DropEvidence]) -> Optional[DropValue]:
    """What the swap weighs a drop candidate at; None = unknown, so never the drop.

    Last season's line (`baseline`) already rests on a real sample. A recent
    average on fewer than MIN_SAMPLE_GAMES games counts at the higher of itself
    and the season projection: a drop is the move that cannot be taken back, so a
    couple of quiet games never make a player look worse than projected. No value
    at all (0.0 on the board) is the projection, and without one a thin or empty
    sample is unknown. If the lookup failed (`evidence` None) the board's averages
    stand, but an empty one still never makes a player the weakest."""
    if p.value_source == "baseline":
        return DropValue(p.avg_points, False)
    if p.value_source is not None and (evidence is None or evidence.games.get(p.player_id, 0) >= MIN_SAMPLE_GAMES):
        return DropValue(p.avg_points, False)
    prior = evidence.projected.get(p.player_id) if evidence is not None else None
    if prior is None:
        return None
    if p.value_source is not None and p.avg_points >= prior:
        return DropValue(p.avg_points, False)
    return DropValue(prior, True)


def suggest_transaction(
    state: LineupState,
    streamers: Sequence[StreamerPlayerResp],
    *,
    ir_eligible: Mapping[int, bool],
    ratio: float = STREAMABLE_RATIO,
    evidence: Optional[DropEvidence] = None,
) -> Optional[TransactionSuggestion]:
    """The day's streamer swap, or None. `streamers` is the daily search in its own
    order (free agents with a game today, best first). The drop is the candidate
    with the lowest `drop_value`. Pickups ESPN would refuse under
    `rosterSettings.positionLimits` are skipped — a swap that drops a player of the
    same default position frees that seat and still counts."""
    roster_ids = {p.player_id for p in state.players}
    drop: Optional[LineupPlayer] = None
    weighed: Optional[DropValue] = None
    if not has_open_seat(state):
        valued = [(v, p) for p in drop_candidates(state, ir_eligible)
                  if (v := drop_value(p, evidence)) is not None]
        if not valued:
            return None
        weighed, drop = min(valued, key=lambda vp: (vp[0].value, vp[1].name))

    limits = {int(k): int(v) for k, v in state.position_limits.items() if int(v) > 0}
    held = Counter(p.default_position_id for p in state.players if p.default_position_id is not None)

    def capped(s: StreamerPlayerResp) -> bool:
        pos = s.default_position_id
        if pos is None or pos not in limits:
            return False
        freed = 1 if drop is not None and drop.default_position_id == pos else 0
        return held[pos] - freed >= limits[pos]

    pickup = next((s for s in streamers
                   if s.acquisition_status == "free_agent" and s.avg_points_last_n is not None
                   and s.player_id not in roster_ids and not capped(s)), None)
    if pickup is None:
        return None
    if drop is None or weighed is None:
        return TransactionSuggestion(pickup, None)
    if pickup.avg_points_last_n < weighed.value * ratio:
        return None
    return TransactionSuggestion(pickup, drop, weighed)


def _of_roster(p: LineupPlayer) -> DailyActionPlayer:
    return DailyActionPlayer(
        player_id=p.player_id, nba_player_id=p.nba_player_id, name=p.name, team=p.team,
        injury_status=p.injury_status, lineup_slot_id=p.lineup_slot_id, lineup_slot=p.lineup_slot,
        avg_points=p.avg_points,
    )


def _of_pickup(s: StreamerPlayerResp) -> DailyActionPlayer:
    return DailyActionPlayer(
        player_id=s.player_id, nba_player_id=s.nba_player_id, name=s.name, team=s.team,
        injury_status=s.injury_status, avg_points=s.avg_points_last_n,
    )


def build_actions(
    state: LineupState,
    fill: Plan,
    ir: Sequence[IrAction],
    suggestion: Optional[TransactionSuggestion],
    *,
    pickup_game: Optional[tuple[str, Optional[str]]] = None,   # ("vs LAL · 7:30 PM", "19:30")
    drop_next: Optional[str] = None,                           # "next plays Thu"
) -> list[DailyAction]:
    """Rows, copy and order: IR housekeeping first, then starts by tip-off, the pickup last."""
    by_id = {p.player_id: p for p in state.players}
    out: list[DailyAction] = []

    for action in ir:
        p = by_id[action.player_id]
        m = action.move
        if action.kind == "ir_in" and m is not None:
            out.append(DailyAction(
                id=f"ir_in:{p.player_id}", kind="ir_in", title=f"Move {p.name} to IR",
                detail=f"{m.note} · frees {p.lineup_slot}", player=_of_roster(p),
                moves=move_results([m], state), game_time_et=p.game_time_et,
            ))
        elif m is not None:
            out.append(DailyAction(
                id=f"ir_out:{p.player_id}", kind="ir_out", title=f"Move {p.name} off IR",
                detail=f"Healthy · to {_slot_name(m.to_slot_id)}", player=_of_roster(p),
                moves=move_results([m], state), game_time_et=p.game_time_et,
            ))
        else:
            out.append(DailyAction(
                id=f"ir_out:{p.player_id}", kind="ir_out", title=f"Move {p.name} off IR",
                detail=ROSTER_FULL_DETAIL, player=_of_roster(p), blocked_reason="roster_full",
                game_time_et=p.game_time_et,
            ))

    for chain in chains_of(list(fill.moves)):
        start = chain[0]
        filler = by_id[start.player_id]
        bench = next((x for x in chain if x.role == "bench"), None)
        evicted = by_id.get(bench.player_id) if bench is not None else None
        detail = " · ".join(x for x in (start.note, _slot_name(start.to_slot_id)) if x)
        if evicted is not None and bench is not None:
            detail += f" over {evicted.name} ({bench.note})"
        out.append(DailyAction(
            id=f"start:{filler.player_id}", kind="start", title=f"Start {filler.name}", detail=detail or None,
            player=_of_roster(filler), counterpart=_of_roster(evicted) if evicted is not None else None,
            moves=move_results(chain, state), game_time_et=filler.game_time_et,
        ))

    if suggestion is not None:
        pick = suggestion.pickup
        label, tip = pickup_game or (None, None)
        plays = f"{pick.name} plays tonight" + (f" {label}" if label else "")
        value = f"{pick.avg_points_last_n:.1f}" if pick.avg_points_last_n is not None else "—"
        if suggestion.drop is None:
            out.append(DailyAction(
                id=f"add:{pick.player_id}", kind="add", title=f"Pick up {pick.name}",
                detail=f"Open roster seat · {plays} · {value} avg", player=_of_pickup(pick),
                transaction=DailyActionTransaction(pickup=pick, drop_player_id=None), game_time_et=tip,
            ))
        else:
            drop = suggestion.drop
            idle = f"{drop.name} has no game today" + (f" ({drop_next})" if drop_next else "")
            weighed = suggestion.drop_value or DropValue(drop.avg_points, False)
            dropped = f"{weighed.value:.1f}" + (" projected" if weighed.projected else "")
            out.append(DailyAction(
                id=f"add_drop:{pick.player_id}:{drop.player_id}", kind="add_drop",
                title=f"Drop {drop.name}, pick up {pick.name}",
                detail=f"{idle} · {plays} · {dropped} vs {value}",
                player=_of_pickup(pick), counterpart=_of_roster(drop),
                transaction=DailyActionTransaction(pickup=pick, drop_player_id=drop.player_id), game_time_et=tip,
            ))

    out.sort(key=lambda a: (KIND_ORDER[a.kind], a.game_time_et or "99:99", a.player.name))
    return out


# ------------------------------- lookups (failure-tolerant) ------------------------------- #


def _drop_evidence(league_info: LeagueInfo, espn_ids: list[int]) -> DropEvidence:
    # The scoring the board's values were computed under (`lineup_read_service._values_for`).
    scoring = EspnService._scoring_for(league_info)
    return DropEvidence(
        games=PlayerValueService.rolling_games_by_espn_id(espn_ids, BOARD_WINDOW_DAYS),
        projected=PlayerValueService.projected_value_by_espn_id(scoring, espn_ids, days=BOARD_WINDOW_DAYS),
    )


async def _drop_evidence_for(league_info: LeagueInfo, candidates: Sequence[LineupPlayer]) -> Optional[DropEvidence]:
    """The candidates' samples and projections; None when the lookup fails, never a failure."""
    try:
        return await run_db("daily_actions.drop_evidence", _drop_evidence, league_info,
                            [p.player_id for p in candidates])
    except Exception as exc:
        log.warning("daily_actions_drop_evidence_failed", error=str(exc))
        return None


async def _pickup_game(pick: StreamerPlayerResp, nba_date: date) -> Optional[tuple[str, Optional[str]]]:
    """("vs LAL · 7:30 PM", "19:30") for the pickup's game today; None when unknown, never a failure."""
    try:
        games = await run_db("daily_actions.games", _games_on, nba_date)
        _, by_team = index_games(games)
    except Exception as exc:
        log.warning("daily_actions_games_lookup_failed", error=str(exc))
        return None
    game = by_team.get(pick.team)
    if game is None:
        return None
    opponent = f"vs {game.away_team_id}" if game.home_team_id == pick.team else f"@ {game.home_team_id}"
    hhmm = game.start_time_et.strftime("%H:%M") if game.start_time_et else None
    when = _clock(hhmm)
    return (f"{opponent}{' · ' + when if when else ''}", hhmm)


def _next_game_label(team: str, pool: StreamerData, streamer_day: date) -> Optional[str]:
    """"next plays Thu" / "no more games this week" for the drop candidate; None when the calendar cannot say."""
    try:
        days = schedule_service.get_remaining_game_days(team, streamer_day)
    except Exception as exc:
        log.warning("daily_actions_calendar_failed", team=team, error=str(exc))
        return None
    today = pool.target_day or 0
    later = [d for d in days if d > today]
    if not later:
        return "no more games this week"
    return f"next plays {(pool.start_date + timedelta(days=min(later))).strftime('%a')}"


# ------------------------------- service ------------------------------- #


class DailyActionsService:

    @staticmethod
    async def read(team, league_info: LeagueInfo, *, fa_count: int = DEFAULT_FA_COUNT,
                   ratio: float = STREAMABLE_RATIO) -> DailyActionsResp:
        adapter = get_provider_adapter(league_info.provider)
        if not adapter.capabilities(league_info).lineup_read:
            return DailyActionsResp(status=ApiStatus.SUCCESS,
                                    message=unavailable_message("daily_actions", league_info.provider),
                                    data=DailyActionsData(can_write=False, write_blocked_reason="provider_not_supported"))

        board, pool = await asyncio.gather(
            adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=_roster_slots(team)),
            StreamerService.find_streamers(
                league_info, fa_count=fa_count, exclude_injured=True, b2b_only=False,
                mode=StreamerMode.DAILY, target_day=None, avg_days=7, team_id=team.team_id,
            ),
            return_exceptions=True,
        )
        if isinstance(board, BaseException):
            raise board
        state: LineupState = board

        pool_data: Optional[StreamerData] = None
        streamers_error: Optional[str] = None
        if isinstance(pool, BaseException):
            streamers_error = str(pool) or type(pool).__name__
            log.warning("daily_actions_streamers_failed", team_id=team.team_id, error=streamers_error)
        else:
            pool_data = pool.data   # None = no matchup on the calendar: an empty pool, not a failure

        players = planner_players(state)
        counts = slot_counts_of(state)
        fill = plan_fill(players, counts)
        ir = plan_ir(players, counts)
        value_kind = state.players[0].value_kind if state.players else "fpts"

        suggestion: Optional[TransactionSuggestion] = None
        pickup_game: Optional[tuple[str, Optional[str]]] = None
        drop_next: Optional[str] = None
        if pool_data is not None:
            streamer_day = pool_data.start_date + timedelta(days=pool_data.target_day or 0)
            board_day = date.fromisoformat(state.nba_date) if state.nba_date else None
            if board_day != streamer_day:
                streamers_error = "day_mismatch"
                log.info("daily_actions_day_mismatch", team_id=team.team_id, board=state.nba_date,
                         streamers=streamer_day.isoformat())
            elif pool_data.value_kind != value_kind:
                streamers_error = "value_kind_mismatch"
            else:
                ir_eligible = {p.player_id: p.ir_eligible for p in players}
                candidates = [] if has_open_seat(state) else drop_candidates(state, ir_eligible)
                evidence = await _drop_evidence_for(league_info, candidates) if candidates else None
                suggestion = suggest_transaction(
                    state, pool_data.streamers, ir_eligible=ir_eligible, ratio=ratio, evidence=evidence,
                )
            if suggestion is not None and board_day is not None:
                pickup_game = await _pickup_game(suggestion.pickup, board_day)
                if suggestion.drop is not None:
                    drop_next = _next_game_label(suggestion.drop.team, pool_data, streamer_day)

        actions = build_actions(state, fill, ir, suggestion, pickup_game=pickup_game, drop_next=drop_next)
        message = f"{len(actions)} action(s) today" if actions else "All set for today"
        return DailyActionsResp(status=ApiStatus.SUCCESS, message=message, data=DailyActionsData(
            lineup=state, roster_version=state.roster_version, scoring_period_id=state.scoring_period_id,
            nba_date=state.nba_date, can_write=state.can_write, write_blocked_reason=state.write_blocked_reason,
            value_kind=value_kind, unfilled=unfilled_results(fill), actions=actions,
            streamers_error=streamers_error,
        ))
