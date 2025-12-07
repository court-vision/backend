from fastapi import APIRouter, Depends
from services.lineup_service import LineupService
from schemas.lineup import GenerateLineupReq, SaveLineupReq, GetLineupsResp, SaveLineupResp, DeleteLineupResp, GenerateLineupResp
from core.security import get_current_user

router = APIRouter(prefix="/lineups", tags=["lineup management"])

@router.post('/generate', response_model=GenerateLineupResp)
async def generate_lineup(req: GenerateLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await LineupService.generate_lineup(user_id, req.team_id, req.threshold, req.week)

@router.get('', response_model=GetLineupsResp)
async def get_lineups(team_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await LineupService.get_lineups(user_id, team_id)

@router.put('/save', response_model=SaveLineupResp)
async def save_lineup(req: SaveLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await LineupService.save_lineup(user_id, req.team_id, req.lineup_info)

@router.delete('/remove', response_model=DeleteLineupResp)
async def remove_lineup(lineup_id: int, current_user: dict = Depends(get_current_user)):
    return await LineupService.remove_lineup(lineup_id)
