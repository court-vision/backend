from fastapi import APIRouter, Depends, HTTPException
from services.lineup_service import LineupService
from schemas.lineup import GenerateLineupReq, SaveLineupReq, GetLineupsResp, SaveLineupResp, DeleteLineupResp, GenerateLineupResp
from api.deps import get_db_user, get_owned_lineup, ensure_team_owned
from db.models.users import User
from db.models.lineups import Lineup

router = APIRouter(prefix="/lineups", tags=["lineup management"])


@router.post('/generate', response_model=GenerateLineupResp)
async def generate_lineup(req: GenerateLineupReq, user: User = Depends(get_db_user)):
    return await LineupService.generate_lineup(user.user_id, req.team_id, req.streaming_slots, req.week, req.avg_mode)

@router.get('', response_model=GetLineupsResp)
async def get_lineups(team_id: int, user: User = Depends(get_db_user)):
    return await LineupService.get_lineups(user.user_id, team_id)

@router.put('/save', response_model=SaveLineupResp)
async def save_lineup(req: SaveLineupReq, user: User = Depends(get_db_user)):
    if ensure_team_owned(req.team_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return await LineupService.save_lineup(user.user_id, req.team_id, req.lineup_info)

@router.delete('/remove', response_model=DeleteLineupResp)
async def remove_lineup(lineup: Lineup = Depends(get_owned_lineup)):
    return await LineupService.remove_lineup(lineup.lineup_id)
