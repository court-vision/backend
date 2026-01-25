from typing import Optional
from fastapi import APIRouter, Query
from schemas.player import PlayerStatsResp
from services.player_service import PlayerService

router = APIRouter(prefix="/players", tags=["players"])


@router.get('/stats', response_model=PlayerStatsResp)
async def get_player_stats_by_query(
    espn_id: Optional[int] = Query(None, description="ESPN player ID (preferred for internal lookups)"),
    player_id: Optional[int] = Query(None, description="Player ID (alias for espn_id, for backwards compatibility)"),
    name: Optional[str] = Query(None, description="Player name (used for public/roster lookup)"),
    team: Optional[str] = Query(None, description="Player team abbreviation (used with name)")
) -> PlayerStatsResp:
    """
    Get player stats by ID or by name/team combination.

    Lookup priority:
    1. espn_id - ESPN player ID (most reliable for internal use)
    2. player_id - Alias for espn_id (backwards compatibility)
    3. name + team - Name-based lookup (for public queries)
    """
    return await PlayerService.get_player_stats(espn_id=espn_id, player_id=player_id, name=name, team=team)


@router.get('/{player_id}/stats', response_model=PlayerStatsResp)
async def get_player_stats(player_id: int) -> PlayerStatsResp:
    """Get player stats by ID (legacy endpoint for backwards compatibility)."""
    return await PlayerService.get_player_stats(player_id=player_id)

