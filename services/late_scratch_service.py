"""
The late-scratch swap (the news plan's late-scratch pass, step 3b; the Late scratches
row's Act level in Dime's settings table): when the availability pipeline's verdict
on a player changes to out, doubtful or available before his game, data-platform
posts the change here once (POST /v1/internal/jobs/lineup/react) and this decides,
team by team, whether the lineup moves.

For each team whose Late scratches level is notify or act (`late_scratch_level`, the
team's override else the account's; services/ai/levels.py). A team at AI is left to
Dime's event check-ins (services/ai/events.py), so it is not read here:

1. Read today's board. Skip unless it is the verdict's day and the player is on it,
   unlocked, with a game, and starting (out, doubtful) or on the bench (available).
2. Put our verdicts on the board (nba.player_availability for the day; the posted
   change wins for him) and plan with `plan_reaction`: only the chain that benches
   him or starts him. A player is never benched just for being out: only for a
   better-tier bench player with a game. Nothing else on the board moves, so a
   hand-set lineup is not re-planned by a news event.
3. At notify it is a plan (the email suggests it). At act, write it through the lineup
   write chain as source 'availability' with his NBA id: once per player per team per
   day (migration 0035's partial unique index) and at most LATE_SCRATCH_SWAPS_PER_DAY a
   team a day. ROSTER_WRITES_ENABLED off, or the board not writable, makes it a plan.

Every team's outcome comes back (applied, planned, noop, skipped with why, rejected,
failed), never raised, so data-platform can email the owner what happened and why: an
error in one team's pass is that team's `failed` (`unexpected_error`), and one raised
by the write itself is `write_uncertain`, since ESPN may have taken the lineup before
it. One team's count-and-write runs at a time (`_team_locks`), so two changes answered
at once can't both pass the daily cap or the once-per-player check.
It does not undo a swap when a benched player is upgraded again (tier A never
displaces tier A, and the once-per-player index holds), touch IR, or add or drop.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date
from typing import Optional

from core.errors import ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupReactData, LineupReactReq, LineupReactResp, LineupReactTeam, LineupState
from services.ai import autonomy
from services.lineup_editor_service import (
    LineupEditorService,
    RosterWriteRejected,
    RosterWriteUnavailable,
    _load_team_for_job,
    move_results,
    planner_players,
    slot_counts_of,
)
from services.lineup_planner import ACTIVE_SLOT_IDS, BENCH_SLOT_ID, plan_reaction
from services.providers import get_provider_adapter

log = get_logger("late_scratch")

SOURCE = "availability"

# One team's count-and-write at a time. This process is the only writer (serve.py runs one
# uvicorn server), so a lock per team keeps two overlapping changes from both passing the cap.
_team_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ------------------------------- DB (run in the executor) ------------------------------- #


LEVELS = (autonomy.NOTIFY, autonomy.ACT)


def _teams() -> list[tuple[int, int, str]]:
    """(team_id, user_id, level) of every team whose Late scratches level is notify or act and
    not at AI: the team's override, else its account's level."""
    from db.models.autopilot import Autopilot
    from db.models.notifications import NotificationPreference, NotificationTeamPreference
    from db.models.teams import Team

    account = {int(r.user_id): r.late_scratch_level for r in
               NotificationPreference.select(NotificationPreference.user, NotificationPreference.late_scratch_level)
               .where(NotificationPreference.late_scratch_level.in_(LEVELS))}
    override = {(int(r.user_id), int(r.team_id)): r.late_scratch_level for r in
                NotificationTeamPreference.select(NotificationTeamPreference.user, NotificationTeamPreference.team_id,
                                                  NotificationTeamPreference.late_scratch_level)
                .where(NotificationTeamPreference.late_scratch_level.is_null(False))}
    users = set(account) | {u for (u, _), level in override.items() if level in LEVELS}
    if not users:
        return []
    out: list[tuple[int, int, str]] = []
    for r in Team.select(Team.team_id, Team.user_id).where(Team.user_id.in_(sorted(users))).order_by(Team.team_id):
        team_id, user_id = int(r.team_id), int(r.user_id_id)
        level = override.get((user_id, team_id)) or account.get(user_id)
        if level in LEVELS:
            out.append((team_id, user_id, level))
    if not out:
        return []
    at_ai = {int(r.team_id_id) for r in Autopilot.select().where(Autopilot.team_id.in_([t for t, _, _ in out]))
             if autonomy.at_ai(r, autonomy.LATE_SCRATCH)}
    return [t for t in out if t[0] not in at_ai]


def _verdicts(nba_ids: list[int], game_date: date) -> dict[int, tuple[str, Optional[str]]]:
    """Our verdict and the one before it for each of these players on that day."""
    from db.models.nba.availability import PlayerAvailability
    if not nba_ids:
        return {}
    rows = (PlayerAvailability
            .select(PlayerAvailability.player, PlayerAvailability.verdict, PlayerAvailability.previous_verdict)
            .where(PlayerAvailability.player.in_(nba_ids) & (PlayerAvailability.game_date == game_date)))
    return {int(r.player_id): (r.verdict, r.previous_verdict) for r in rows}


def _swaps_today(team_id: int, nba_date: date) -> tuple[int, set[int]]:
    """This team's counted swaps today, and the players they answered."""
    from db.models.roster_moves import COUNTED_AUTO_STATUSES, RosterMove
    rows = (RosterMove.select(RosterMove.trigger_player_id)
            .where((RosterMove.team == team_id) & (RosterMove.nba_date == nba_date)
                   & (RosterMove.source == SOURCE) & (RosterMove.status.in_(COUNTED_AUTO_STATUSES))))
    triggers = [r.trigger_player_id for r in rows]
    return len(triggers), {int(t) for t in triggers if t is not None}


# ------------------------------- service ------------------------------- #


class LateScratchService:

    @staticmethod
    async def react(req: LineupReactReq) -> LineupReactResp:
        teams = await run_db("late_scratch.teams", _teams)
        results: list[LineupReactTeam] = []
        for team_id, user_id, level in teams:
            try:
                results.append(await LateScratchService._team(req, team_id, user_id, level))
            except Exception as exc:  # one team's failure must not cost the rest their results
                log.error("late_scratch_team_failed", team_id=team_id, player_id=req.player_id, error=str(exc))
                results.append(LineupReactTeam(team_id=team_id, user_id=user_id, level=level,
                                               outcome="failed", reason="unexpected_error"))
        swapped = sum(1 for r in results if r.outcome == "applied")
        log.info("late_scratch_react", player_id=req.player_id, verdict=req.verdict,
                 previous_verdict=req.previous_verdict, source=req.source, teams=len(results), applied=swapped)
        return LineupReactResp(
            status=ApiStatus.SUCCESS,
            message=f"{swapped} of {len(results)} team(s) swapped",
            data=LineupReactData(player_id=req.player_id, verdict=req.verdict, teams=results),
        )

    @staticmethod
    async def _team(req: LineupReactReq, team_id: int, user_id: int, level: str) -> LineupReactTeam:
        state: Optional[LineupState] = None
        trigger_name: Optional[str] = None

        def done(outcome: str, *, reason: Optional[str] = None, moves=None, verified: Optional[bool] = None,
                 audit_id: Optional[int] = None, board: Optional[LineupState] = None) -> LineupReactTeam:
            board = board or state
            if outcome != "skipped" or reason not in ("not_rostered", "not_today"):
                log.info("late_scratch_team", team_id=team_id, player_id=req.player_id, verdict=req.verdict,
                         level=level, outcome=outcome, reason=reason, move_count=len(moves or []))
            return LineupReactTeam(
                team_id=team_id, user_id=user_id, level=level, outcome=outcome, reason=reason,
                player_name=trigger_name,
                moves=moves or [], verified=verified, audit_id=audit_id,
                nba_date=board.nba_date if board else None,
                first_game_time_et=board.first_game_time_et if board else None,
                team_name=board.team_name if board else "",
            )

        try:
            league_info, roster_slots = await run_db("late_scratch.team", _load_team_for_job, team_id, user_id)
            adapter = get_provider_adapter(league_info.provider)
            if not adapter.capabilities(league_info).lineup_read:
                return done("skipped", reason="provider_not_supported")
            state = await adapter.read_lineup(team_id, league_info, fallback_slot_counts=roster_slots)
        except ProviderAuthError as exc:
            return done("rejected", reason=f"PROVIDER_AUTH_EXPIRED: {exc.message}")
        except Exception as exc:  # one team's read must not cost the rest theirs
            log.warning("late_scratch_read_failed", team_id=team_id, error=str(exc))
            return done("failed", reason="read_failed")

        if state.nba_date != req.game_date.isoformat():
            return done("skipped", reason="not_today")
        trigger = next((p for p in state.players if p.nba_player_id == req.player_id), None)
        if trigger is None:
            return done("skipped", reason="not_rostered")
        trigger_name = trigger.name
        if trigger.locked:
            return done("skipped", reason="locked")
        if not trigger.has_game_today:
            return done("skipped", reason="no_game")
        if req.verdict == "available":
            if trigger.lineup_slot_id != BENCH_SLOT_ID:
                return done("skipped", reason="not_on_bench")
        elif trigger.lineup_slot_id not in ACTIVE_SLOT_IDS:
            return done("skipped", reason="not_starting")

        nba_ids = sorted({p.nba_player_id for p in state.players if p.nba_player_id is not None})
        verdicts = await run_db("late_scratch.verdicts", _verdicts, nba_ids, req.game_date)
        verdicts[req.player_id] = (req.verdict, req.previous_verdict)  # the change being answered is the newest
        plan = plan_reaction(planner_players(state, verdicts), slot_counts_of(state), trigger.player_id)
        if plan.is_noop:
            return done("noop", reason="no_better_bench_player" if req.verdict != "available" else "no_slot")
        moves = move_results(list(plan.moves), state)
        if level != autonomy.ACT:
            return done("planned", reason="notify", moves=moves)
        if not settings.roster_writes_enabled:
            return done("planned", reason="writes_disabled", moves=moves)
        if not state.can_write:
            return done("planned", reason=f"can_write:{state.write_blocked_reason}", moves=moves)

        async with _team_locks[team_id]:
            count, answered = await run_db("late_scratch.swaps_today", _swaps_today, team_id, req.game_date)
            if req.player_id in answered:
                return done("skipped", reason="already_swapped", moves=moves)
            if count >= settings.late_scratch_swaps_per_day:
                return done("skipped", reason="daily_cap", moves=moves)

            try:
                fresh, verified, audit_id = await LineupEditorService._write(
                    user_id=user_id, team_id=team_id, league_info=league_info, state=state,
                    moves=list(plan.moves), source=SOURCE, fallback_slot_counts=roster_slots,
                    trigger_player_id=req.player_id,
                )
            except RosterWriteRejected as exc:
                return done("rejected", reason=exc.message, moves=moves)
            except ProviderAuthError as exc:
                return done("rejected", reason=f"PROVIDER_AUTH_EXPIRED: {exc.message}", moves=moves)
            except RosterWriteUnavailable as exc:
                return done("failed", reason=exc.message, moves=moves)
            except Exception as exc:  # e.g. the audit update after ESPN took it: the swap may stand
                log.error("late_scratch_write_uncertain", team_id=team_id, player_id=req.player_id, error=str(exc))
                return done("failed", reason="write_uncertain", moves=moves)
        return done("applied", moves=moves, verified=verified, audit_id=audit_id, board=fresh)
