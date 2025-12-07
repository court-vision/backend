from fastapi import APIRouter
from schemas.player import PlayerStatsResp
from services.player_service import PlayerService

router = APIRouter(prefix="/players", tags=["players"])


@router.get('/{player_id}/stats', response_model=PlayerStatsResp)
async def get_player_stats(player_id: int) -> PlayerStatsResp:
    return await PlayerService.get_player_stats(player_id)

