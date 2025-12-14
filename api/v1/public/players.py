from typing import Optional
from fastapi import APIRouter, Query
from schemas.player import PlayerStatsResp
from services.player_service import PlayerService

router = APIRouter(prefix="/players", tags=["players"])


@router.get('/stats', response_model=PlayerStatsResp)
async def get_player_stats_by_query(
    player_id: Optional[int] = Query(None, description="NBA player ID (used for standings)"),
    name: Optional[str] = Query(None, description="Player name (used for roster lookup)"),
    team: Optional[str] = Query(None, description="Player team abbreviation (used with name)")
) -> PlayerStatsResp:
    """Get player stats by ID or by name/team combination."""
    return await PlayerService.get_player_stats(player_id=player_id, name=name, team=team)


@router.get('/{player_id}/stats', response_model=PlayerStatsResp)
async def get_player_stats(player_id: int) -> PlayerStatsResp:
    """Get player stats by ID (legacy endpoint for backwards compatibility)."""
    return await PlayerService.get_player_stats(player_id=player_id)

