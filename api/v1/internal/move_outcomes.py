"""
A saved team's moves and what they earned: the move ledger (usr.move_outcomes),
rebuilt nightly from the lineup snapshots. Clerk-authenticated; ownership via
`get_owned_team`. `scope=league` reads every team in the SAME league (the
rivals' moves too) and nothing else.
"""

from datetime import date as date_type
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from api.deps import OwnedTeamContext, get_owned_team
from api.v1.internal.lineup_snapshots import CALENDAR_DAY, DATE_PATTERN
from core.responses import respond
from schemas.move_outcomes import MoveOutcomeListResp, MoveScope, MoveSource
from services.move_ledger_service import MoveLedgerService

router = APIRouter(prefix="/teams", tags=["move ledger"])


@router.get("/{team_id}/move-outcomes", response_model=MoveOutcomeListResp)
async def list_move_outcomes(
    scope: MoveScope = Query("team", description="team: this team's moves; league: every team's in its league"),
    source: Annotated[Optional[list[MoveSource]], Query(description="Only these sources")] = None,
    player_id: Optional[int] = Query(None, gt=0, description="Only moves that added or dropped this NBA player"),
    since: Annotated[Optional[str], Query(pattern=DATE_PATTERN, description="First day (YYYY-MM-DD)"),
                     CALENDAR_DAY] = None,
    limit: int = Query(50, ge=1, le=200),
    team: OwnedTeamContext = Depends(get_owned_team),
) -> MoveOutcomeListResp:
    """The moves, newest first, each with its day, matchup and two-week results, and a
    summary per source (the desk, scheduled pickups, stream lanes, Dime, outside)."""
    return respond(await MoveLedgerService.for_team(
        team, scope=scope, sources=tuple(source or ()), nba_player_id=player_id,
        since=date_type.fromisoformat(since) if since else None, limit=limit,
    ))
