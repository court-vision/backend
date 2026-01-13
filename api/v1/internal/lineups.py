from fastapi import APIRouter, Depends
from services.lineup_service import LineupService
from services.user_sync_service import UserSyncService
from schemas.lineup import GenerateLineupReq, SaveLineupReq, GetLineupsResp, SaveLineupResp, DeleteLineupResp, GenerateLineupResp
from core.clerk_auth import get_current_user

router = APIRouter(prefix="/lineups", tags=["lineup management"])


def _get_user_id(current_user: dict) -> int:
    """Helper to get local user_id from Clerk user info."""
    clerk_user_id = current_user.get("clerk_user_id")
    email = current_user.get("email")
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return user.user_id


@router.post('/generate', response_model=GenerateLineupResp)
async def generate_lineup(req: GenerateLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await LineupService.generate_lineup(user_id, req.team_id, req.threshold, req.week)

@router.get('', response_model=GetLineupsResp)
async def get_lineups(team_id: int, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await LineupService.get_lineups(user_id, team_id)

@router.put('/save', response_model=SaveLineupResp)
async def save_lineup(req: SaveLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await LineupService.save_lineup(user_id, req.team_id, req.lineup_info)

@router.delete('/remove', response_model=DeleteLineupResp)
async def remove_lineup(lineup_id: int, _: dict = Depends(get_current_user)):
    return await LineupService.remove_lineup(lineup_id)
