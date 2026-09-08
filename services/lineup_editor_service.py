"""
Lineup edits: read the board, validate or plan moves, send them to ESPN through
the private fantasy-writer, verify by re-reading, and audit in usr.roster_moves.

Three entry points share one write chain:

    read_state    GET  /teams/{id}/lineup         the editor's board
    plan_today    GET  /teams/{id}/lineup/plan    fill-only suggestion (never writes)
    apply_manual  POST /teams/{id}/lineup/moves   the user's own moves
    evaluate      POST /jobs/lineup/evaluate      the alerts pipeline: plan, and
                                                  apply it for auto-lineup users

Business failures raise the `Roster*` errors below (rendered by the global
handlers with real statuses — see core/responses.ERROR_CODE_STATUS); `evaluate`
catches them and reports an outcome instead, because the pipeline needs a
row per team, not an exception.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Optional

from core.errors import (
    AppError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ProviderAuthError,
    ServiceUnavailableError,
)
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import (
    ApplyLineupMovesData,
    ApplyLineupMovesReq,
    ApplyLineupMovesResp,
    LineupEvaluateReq,
    LineupEvaluationData,
    LineupEvaluationResp,
    LineupMoveResult,
    LineupPlanData,
    LineupPlanResp,
    LineupState,
    LineupStateResp,
    LineupUnfilled,
    MoveErrorResp,
)
from services import fantasy_writer_client
from services.fantasy_writer_client import (
    FantasyWriterAuthRejected,
    FantasyWriterRejected,
    FantasyWriterUnavailable,
)
from services.lineup_planner import Move, Plan, PlannerPlayer, plan_fill, validate_moves
from services.lineup_read_service import LineupReadService
from utils.espn_helpers import POSITION_MAP

log = get_logger("lineup_editor")

NOT_ESPN_MESSAGE = "Lineup editing is available for ESPN teams"


# ------------------------------- errors ------------------------------- #


class RosterWriteDisabled(AuthorizationError):
    error_code = "ROSTER_WRITE_DISABLED"
    default_message = "Lineup changes from Court Vision are switched off right now"


class RosterWriteBlocked(ConflictError):
    error_code = "ROSTER_WRITE_BLOCKED"
    default_message = "This lineup cannot be changed from Court Vision"


class RosterStale(ConflictError):
    error_code = "ROSTER_STALE"
    default_message = "Your lineup changed since this page loaded — review the refreshed roster"


class RosterWriteRejected(ConflictError):
    error_code = "ROSTER_WRITE_REJECTED"
    default_message = "ESPN rejected the lineup change"


class RosterMoveInvalid(AppError):
    status_code = 422
    api_status = ApiStatus.VALIDATION_ERROR
    error_code = "ROSTER_MOVE_INVALID"
    default_message = "Those moves are not allowed"
    log_level = "info"


class RosterWriteUnavailable(ServiceUnavailableError):
    error_code = "ROSTER_WRITE_UNAVAILABLE"
    default_message = fantasy_writer_client.UNAVAILABLE_MESSAGE


# ------------------------------- shapes ------------------------------- #


def _slot_name(slot_id: int) -> str:
    name = POSITION_MAP.get(slot_id)
    return name if isinstance(name, str) and name else str(slot_id)


def planner_players(state: LineupState) -> list[PlannerPlayer]:
    out: list[PlannerPlayer] = []
    for p in state.players:
        note = None
        if p.has_game_today:
            when = _clock(p.game_time_et)
            note = f"{p.opponent or 'game'}{' · ' + when if when else ''}"
        out.append(PlannerPlayer(
            player_id=p.player_id, name=p.name, slot_id=p.lineup_slot_id,
            eligible_slot_ids=frozenset(p.eligible_slot_ids), has_game=p.has_game_today,
            injury_status=p.injury_status, locked=p.locked, value=p.avg_points, game_note=note,
            injured=p.injured,
        ))
    return out


def _clock(hhmm: Optional[str]) -> Optional[str]:
    if not hhmm:
        return None
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")
    except ValueError:
        return hhmm


def slot_counts_of(state: LineupState) -> dict[int, int]:
    return {int(k): int(v) for k, v in state.slot_counts.items()}


def move_results(moves: list[Move], state: LineupState) -> list[LineupMoveResult]:
    names = {p.player_id: p.name for p in state.players}
    return [LineupMoveResult(
        player_id=m.player_id, name=names.get(m.player_id, str(m.player_id)),
        from_slot_id=m.from_slot_id, from_slot=_slot_name(m.from_slot_id),
        to_slot_id=m.to_slot_id, to_slot=_slot_name(m.to_slot_id),
        role=m.role, note=m.note,
    ) for m in moves]


def unfilled_results(plan: Plan) -> list[LineupUnfilled]:
    return [LineupUnfilled(player_id=u.player_id, name=u.name, slot=_slot_name(u.slot_id), reason=u.reason)
            for u in plan.unfilled]


def _moves_json(moves: list[Move]) -> list[dict[str, Any]]:
    return [{"player_id": m.player_id, "from_slot_id": m.from_slot_id, "to_slot_id": m.to_slot_id,
             "role": m.role, "note": m.note} for m in moves]


def idempotency_key(team_id: int, state: LineupState, moves: list[Move]) -> str:
    digest = hashlib.sha1(json.dumps(_moves_json(moves), sort_keys=True).encode()).hexdigest()[:12]
    return f"{team_id}:{state.scoring_period_id}:{state.roster_version}:{digest}"


def writer_payload(league_info: LeagueInfo, state: LineupState, moves: list[Move], key: str) -> dict[str, Any]:
    return {
        "season": league_info.year,
        "league_id": league_info.league_id,
        "espn_team_id": state.espn_team_id,
        "member_id": league_info.swid,
        "credentials": {"espn_s2": league_info.espn_s2, "swid": league_info.swid},
        "scoring_period_id": state.scoring_period_id,
        "moves": [{"player_id": m.player_id, "from_slot_id": m.from_slot_id, "to_slot_id": m.to_slot_id} for m in moves],
        "idempotency_key": key,
    }


# ------------------------------- DB (run in the executor) ------------------------------- #


def _audit_insert(user_id: int, team_id: int, nba_date: date, period: Optional[int], source: str,
                  moves: list[dict], key: Optional[str]) -> int:
    from db.models.roster_moves import RosterMove
    row = RosterMove.create(user=user_id, team=team_id, nba_date=nba_date, scoring_period_id=period,
                            source=source, status="failed", moves=moves, error="in_flight", idempotency_key=key)
    return int(row.id)


def _audit_update(audit_id: int, status: str, *, provider_status: Optional[int] = None,
                  error: Optional[str] = None) -> None:
    from db.models.roster_moves import RosterMove
    (RosterMove.update(status=status, provider_status=provider_status, error=error)
     .where(RosterMove.id == audit_id).execute())


def _auto_counted_today(team_id: int, nba_date: date) -> bool:
    from db.models.roster_moves import COUNTED_AUTO_STATUSES, RosterMove
    return (RosterMove.select()
            .where((RosterMove.team == team_id) & (RosterMove.nba_date == nba_date)
                   & (RosterMove.source == "auto") & (RosterMove.status.in_(COUNTED_AUTO_STATUSES)))
            .exists())


def _load_team_for_job(team_id: int, user_id: int) -> tuple[LeagueInfo, dict[str, int]]:
    """League info WITH credentials plus the synced roster slots, for a token-authed job."""
    from db.models.teams import Team
    from services.team_service import TeamService
    team = Team.get_or_none((Team.team_id == team_id) & (Team.user_id == user_id))
    if team is None:
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
    league_info = TeamService.deserialize_league_info(json.loads(team.league_info), team)
    league = team.league if team.league_id is not None else None
    return league_info, dict(getattr(league, "roster_slots", None) or {})


# ------------------------------- service ------------------------------- #


class LineupEditorService:

    # ---- reads ----

    @staticmethod
    async def read_state(team, league_info: LeagueInfo) -> LineupStateResp:
        if league_info.provider != FantasyProvider.ESPN:
            return LineupStateResp(status=ApiStatus.SUCCESS, message=NOT_ESPN_MESSAGE, data=None)
        state = await LineupReadService.read(team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
        return LineupStateResp(status=ApiStatus.SUCCESS, message="Lineup fetched", data=state)

    @staticmethod
    async def plan_today(team, league_info: LeagueInfo) -> LineupPlanResp:
        if league_info.provider != FantasyProvider.ESPN:
            return LineupPlanResp(status=ApiStatus.SUCCESS, message=NOT_ESPN_MESSAGE, data=None)
        state = await LineupReadService.read(team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
        plan = plan_fill(planner_players(state), slot_counts_of(state))
        return LineupPlanResp(status=ApiStatus.SUCCESS, message=plan.summary, data=LineupPlanData(
            moves=move_results(list(plan.moves), state), unfilled=unfilled_results(plan), summary=plan.summary,
            scoring_period_id=state.scoring_period_id, nba_date=state.nba_date, roster_version=state.roster_version,
        ))

    # ---- manual write ----

    @staticmethod
    async def apply_manual(team, league_info: LeagueInfo, req: ApplyLineupMovesReq) -> ApplyLineupMovesResp:
        if not settings.roster_writes_enabled:
            raise RosterWriteDisabled()
        if league_info.provider != FantasyProvider.ESPN:
            raise RosterWriteBlocked(message=NOT_ESPN_MESSAGE, data={"reason": "provider_not_supported"})

        state = await LineupReadService.read(team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
        if not state.can_write:
            raise RosterWriteBlocked(data={"reason": state.write_blocked_reason})
        if state.roster_version != req.roster_version or state.scoring_period_id != req.expected_scoring_period_id:
            raise RosterStale(data={"lineup": state.model_dump(mode="json")})

        moves = [Move(m.player_id, m.from_slot_id, m.to_slot_id) for m in req.moves]
        errors = validate_moves(planner_players(state), slot_counts_of(state), moves)
        if errors:
            raise RosterMoveInvalid(data={"errors": [
                MoveErrorResp(player_id=e.player_id, code=e.code, message=e.message).model_dump() for e in errors]})

        fresh, verified, audit_id = await LineupEditorService._write(
            user_id=team.user_id, team_id=team.team_id, league_info=league_info, state=state,
            moves=moves, source="manual", fallback_slot_counts=_roster_slots(team),
        )
        applied = _describe(moves, state, fresh)
        return ApplyLineupMovesResp(
            status=ApiStatus.SUCCESS,
            message=f"{len(moves)} move(s) sent to ESPN" + ("" if verified else " — not yet confirmed"),
            data=ApplyLineupMovesData(lineup=fresh, applied_moves=applied, verified=verified, audit_id=audit_id),
        )

    # ---- pipeline route ----

    @staticmethod
    async def evaluate(req: LineupEvaluateReq) -> LineupEvaluationResp:
        league_info, roster_slots = await run_db("lineup.job_team", _load_team_for_job, req.team_id, req.user_id)

        def done(outcome: str, *, reason: Optional[str] = None, state: Optional[LineupState] = None,
                 plan: Optional[Plan] = None, verified: Optional[bool] = None,
                 audit_id: Optional[int] = None) -> LineupEvaluationResp:
            moves = move_results(list(plan.moves), state) if plan and state else []
            data = LineupEvaluationData(
                outcome=outcome, reason=reason, moves=moves,
                unfilled=unfilled_results(plan) if plan else [], verified=verified,
                scoring_period_id=state.scoring_period_id if state else None,
                nba_date=state.nba_date if state else None,
                first_game_time_et=state.first_game_time_et if state else None,
                team_name=state.team_name if state else league_info.team_name, audit_id=audit_id,
            )
            log.info("lineup_evaluate", team_id=req.team_id, apply=req.apply, outcome=outcome, reason=reason,
                     move_count=len(moves))
            return LineupEvaluationResp(status=ApiStatus.SUCCESS, message=f"Lineup {outcome}", data=data)

        if league_info.provider != FantasyProvider.ESPN:
            return done("skipped", reason="provider_not_supported")

        state = await LineupReadService.read(req.team_id, league_info, fallback_slot_counts=roster_slots)
        plan = plan_fill(planner_players(state), slot_counts_of(state))
        if plan.is_noop:
            if req.apply and state.can_write and state.nba_date:
                # Recorded so the daily dedup sees today as handled
                await run_db("lineup.audit_noop", _audit_noop, req.user_id, req.team_id,
                             date.fromisoformat(state.nba_date), state.scoring_period_id)
            return done("noop", state=state, plan=plan)

        apply = req.apply
        if apply and not settings.roster_writes_enabled:
            apply = False  # alerts keep flowing while writes are switched off
        if not apply:
            return done("planned", state=state, plan=plan)

        if not state.can_write:
            return done("skipped", reason=f"can_write:{state.write_blocked_reason}", state=state, plan=plan)
        if state.nba_date != req.nba_date.isoformat():
            return done("skipped", reason="date_mismatch", state=state, plan=plan)
        if await run_db("lineup.auto_counted", _auto_counted_today, req.team_id, req.nba_date):
            return done("skipped", reason="already_applied_today", state=state, plan=plan)

        try:
            fresh, verified, audit_id = await LineupEditorService._write(
                user_id=req.user_id, team_id=req.team_id, league_info=league_info, state=state,
                moves=list(plan.moves), source="auto", fallback_slot_counts=roster_slots,
            )
        except RosterWriteRejected as exc:
            return done("rejected", reason=exc.message, state=state, plan=plan)
        except ProviderAuthError as exc:
            return done("rejected", reason=f"PROVIDER_AUTH_EXPIRED: {exc.message}", state=state, plan=plan)
        except RosterWriteUnavailable as exc:
            return done("failed", reason=exc.message, state=state, plan=plan)
        return done("applied", state=fresh, plan=plan, verified=verified, audit_id=audit_id)

    # ---- the write chain ----

    @staticmethod
    async def _write(*, user_id: int, team_id: int, league_info: LeagueInfo, state: LineupState,
                     moves: list[Move], source: str, fallback_slot_counts) -> tuple[LineupState, bool, int]:
        if not state.nba_date or not state.scoring_period_id:
            raise RosterWriteBlocked(data={"reason": "no_scoring_period"})
        key = idempotency_key(team_id, state, moves)
        audit_id = await run_db("lineup.audit_insert", _audit_insert, user_id, team_id,
                                date.fromisoformat(state.nba_date), state.scoring_period_id, source,
                                _moves_json(moves), key)
        try:
            result = await fantasy_writer_client.apply_lineup(writer_payload(league_info, state, moves, key))
        except FantasyWriterRejected as exc:
            await run_db("lineup.audit_update", _audit_update, audit_id, "rejected",
                         provider_status=exc.espn_status, error=exc.message)
            raise RosterWriteRejected(message=exc.message, data={"espn_status": exc.espn_status,
                                                                  "espn_error_code": exc.espn_error_code}) from exc
        except FantasyWriterAuthRejected as exc:
            await run_db("lineup.audit_update", _audit_update, audit_id, "failed",
                         provider_status=exc.espn_status, error="provider_auth_rejected")
            raise ProviderAuthError("espn") from exc
        except FantasyWriterUnavailable as exc:
            await run_db("lineup.audit_update", _audit_update, audit_id, "failed",
                         provider_status=exc.espn_status, error=exc.message)
            raise RosterWriteUnavailable() from exc

        try:
            fresh = await LineupReadService.read(team_id, league_info, fallback_slot_counts=fallback_slot_counts)
        except Exception as exc:
            # ESPN took the write and only the read-back failed. Leaving the row at its in_flight
            # `failed` seed would drop it out of the counted statuses, and the next auto run would
            # send the same moves a second time — so settle it as applied, just unverified.
            await run_db("lineup.audit_update", _audit_update, audit_id, "applied_unverified",
                         provider_status=result.espn_status, error=f"verify_read_failed: {exc}")
            log.warning("lineup_write_unverified", team_id=team_id, source=source, move_count=len(moves),
                        espn_status=result.espn_status, error=str(exc))
            return state, False, audit_id

        after = {p.player_id: p.lineup_slot_id for p in fresh.players}
        verified = all(after.get(m.player_id) == m.to_slot_id for m in moves)
        await run_db("lineup.audit_update", _audit_update, audit_id,
                     "applied" if verified else "applied_unverified", provider_status=result.espn_status)
        log.info("lineup_write_applied", team_id=team_id, source=source, move_count=len(moves), verified=verified,
                 espn_status=result.espn_status, idempotent_replay=result.idempotent_replay)
        return fresh, verified, audit_id


def _audit_noop(user_id: int, team_id: int, nba_date: date, period: Optional[int]) -> None:
    from db.models.roster_moves import RosterMove
    if _auto_counted_today(team_id, nba_date):
        return
    RosterMove.create(user=user_id, team=team_id, nba_date=nba_date, scoring_period_id=period,
                      source="auto", status="noop", moves=[])


def _describe(moves: list[Move], before: LineupState, after: LineupState) -> list[LineupMoveResult]:
    """The applied moves with roles inferred from the board (manual moves carry none)."""
    results = move_results(moves, before)
    for r in results:
        if r.from_slot_id == 12 and r.to_slot_id != 12:
            r.role = "start"
        elif r.to_slot_id == 12:
            r.role = "bench"
    return results


def _roster_slots(team) -> dict[str, int]:
    league = getattr(team, "league", None)
    return dict(getattr(league, "roster_slots", None) or {})
