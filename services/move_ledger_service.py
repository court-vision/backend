"""
The move ledger's routes: the nightly rebuild (the pipeline-token job) and a team's
moves (the AI desk's "How moves went", and Dime's `get_move_outcomes`). The work
itself is services/move_ledger.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from api.deps import OwnedTeamContext
from core.errors import NotFoundError
from core.logging import get_logger
from db.base import run_db
from db.models.lineup_snapshots import LineupSnapshot
from schemas.common import ApiStatus, FantasyProvider
from schemas.move_outcomes import (
    LedgerLeagueResult,
    LedgerRunData,
    LedgerRunReq,
    LedgerRunResp,
    MoveOutcomeListData,
    MoveOutcomeListResp,
)
from services import move_ledger
from services.lineup_snapshot_service import public_league_info
from services.matchup_days import fantasy_today

log = get_logger("move_ledger")
NOT_FOUND = "MOVE_LEDGER_NOT_FOUND"


def _newest_season(provider_league_id: str) -> Optional[int]:
    row = (
        LineupSnapshot.select(LineupSnapshot.season)
        .where((LineupSnapshot.provider == "espn") & (LineupSnapshot.provider_league_id == provider_league_id))
        .order_by(LineupSnapshot.season.desc())
        .first()
    )
    return row.season if row else None


async def query_for_team(team: OwnedTeamContext, *, scope: str = "team", sources: tuple[str, ...] = (),
                         nba_player_id: Optional[int] = None, since: Optional[date] = None,
                         limit: int = 20) -> move_ledger.OutcomeQuery:
    info = public_league_info(team)
    if info.provider != FantasyProvider.ESPN:
        raise NotFoundError(NOT_FOUND, "The move ledger is kept for ESPN teams only")
    key = move_ledger.league_key_of(info)
    own = await run_db("move_ledger.own_team", move_ledger.own_provider_team_id, key, info)
    return move_ledger.OutcomeQuery(
        team_id=team.team_id, key=key, provider_team_id=own, scope=scope, sources=tuple(sources),
        nba_player_id=nba_player_id, since=since, limit=limit,
    )


class MoveLedgerService:

    @staticmethod
    async def run(req: LedgerRunReq) -> LedgerRunResp:
        """Rebuild the ledger of one league, or of every league with a recent snapshot."""
        if req.provider_league_id:
            season = req.season or await run_db("move_ledger.season", _newest_season, req.provider_league_id)
            keys = [move_ledger.LeagueKey("espn", req.provider_league_id, season)] if season else []
        else:
            keys = await run_db("move_ledger.leagues", move_ledger.active_leagues, fantasy_today())
        results: list[LedgerLeagueResult] = []
        for key in keys:
            try:
                done = await run_db("move_ledger.rebuild", move_ledger.rebuild_league, key)
            except Exception as exc:  # one league's failure must not stop the others
                log.exception("move_ledger_league_failed", league=key.provider_league_id, season=key.season)
                results.append(LedgerLeagueResult(provider_league_id=key.provider_league_id, season=key.season,
                                                  ok=False, error=type(exc).__name__))
                continue
            results.append(LedgerLeagueResult(
                provider_league_id=key.provider_league_id, season=key.season, ok=True,
                days=done.days, moves=done.moves, by_source=done.by_source,
            ))
        failed = sum(1 for r in results if not r.ok)
        return LedgerRunResp(
            status=ApiStatus.SUCCESS,
            message=f"Rebuilt {len(results) - failed} of {len(results)} leagues",
            data=LedgerRunData(leagues=results, rebuilt=len(results) - failed, failed=failed),
        )

    @staticmethod
    async def for_team(team: OwnedTeamContext, *, scope: str = "team", sources: tuple[str, ...] = (),
                       nba_player_id: Optional[int] = None, since: Optional[date] = None,
                       limit: int = 50) -> MoveOutcomeListResp:
        """The team's moves (or its whole league's), newest first, with a per-source summary."""
        q = await query_for_team(team, scope=scope, sources=sources, nba_player_id=nba_player_id,
                                 since=since, limit=limit)
        found = await run_db("move_ledger.read", move_ledger.read_outcomes, q)
        return MoveOutcomeListResp(
            status=ApiStatus.SUCCESS,
            message="Move outcomes retrieved",
            data=MoveOutcomeListData(team_id=team.team_id, scope=scope, **found),
        )
