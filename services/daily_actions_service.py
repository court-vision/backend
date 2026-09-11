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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Optional, Sequence

from core.logging import get_logger
from db.base import run_db
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
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
from services.lineup_read_service import LineupReadService, _games_on
from services.matchup_days import index_games
from services.streamer_service import StreamerService

log = get_logger("daily_actions")

NOT_ESPN_MESSAGE = "Daily actions are available for ESPN teams"
# The drop candidate is "streamable" only when the day's pool holds a comparable player.
STREAMABLE_RATIO = 0.85
DEFAULT_FA_COUNT = 100
KIND_ORDER = {"ir_out": 0, "ir_in": 1, "start": 2, "add_drop": 3, "add": 3}
ROSTER_FULL_DETAIL = "Roster full — drop a player first"


@dataclass(frozen=True)
class TransactionSuggestion:
    pickup: StreamerPlayerResp
    drop: Optional[LineupPlayer]          # None when a roster seat is open


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


def suggest_transaction(
    state: LineupState,
    streamers: Sequence[StreamerPlayerResp],
    *,
    ir_eligible: Mapping[int, bool],
    ratio: float = STREAMABLE_RATIO,
) -> Optional[TransactionSuggestion]:
    """The day's streamer swap, or None. `streamers` is the daily search in its own
    order (free agents with a game today, best first)."""
    roster_ids = {p.player_id for p in state.players}
    pickup = next((s for s in streamers
                   if s.acquisition_status == "free_agent" and s.avg_points_last_n is not None
                   and s.player_id not in roster_ids), None)
    if pickup is None:
        return None
    # Capacity counts every seat incl. IR — the same rule as the add/drop dialog's
    # "open seat"; ESPN stays the authority and refuses an add it will not take.
    if len(state.players) < sum(slot_counts_of(state).values()):
        return TransactionSuggestion(pickup, None)
    droppable = [p for p in state.players
                 if not p.locked and p.lineup_slot_id != IR_SLOT_ID
                 and not ir_eligible.get(p.player_id, False) and not p.has_game_today]
    if not droppable:
        return None
    drop = min(droppable, key=lambda p: (p.avg_points, p.name))
    if pickup.avg_points_last_n < drop.avg_points * ratio:
        return None
    return TransactionSuggestion(pickup, drop)


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
            out.append(DailyAction(
                id=f"add_drop:{pick.player_id}:{drop.player_id}", kind="add_drop",
                title=f"Drop {drop.name}, pick up {pick.name}",
                detail=f"{idle} · {plays} · {drop.avg_points:.1f} vs {value}",
                player=_of_pickup(pick), counterpart=_of_roster(drop),
                transaction=DailyActionTransaction(pickup=pick, drop_player_id=drop.player_id), game_time_et=tip,
            ))

    out.sort(key=lambda a: (KIND_ORDER[a.kind], a.game_time_et or "99:99", a.player.name))
    return out


# ------------------------------- lookups (failure-tolerant) ------------------------------- #


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
        if league_info.provider != FantasyProvider.ESPN:
            return DailyActionsResp(status=ApiStatus.SUCCESS, message=NOT_ESPN_MESSAGE, data=DailyActionsData(
                can_write=False, write_blocked_reason="provider_not_supported"))

        board, pool = await asyncio.gather(
            LineupReadService.read(team.team_id, league_info, fallback_slot_counts=_roster_slots(team)),
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
                suggestion = suggest_transaction(
                    state, pool_data.streamers,
                    ir_eligible={p.player_id: p.ir_eligible for p in players}, ratio=ratio,
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
