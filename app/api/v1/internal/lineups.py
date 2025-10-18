from fastapi import APIRouter, Depends
from app.services.lineup_service import LineupService
from app.schemas.lineup import GenerateLineupReq, SaveLineupReq, GetLineupsResp, SaveLineupResp, DeleteLineupResp
from app.schemas.common import success_response
from app.core.security import get_current_user
from app.utils.constants import FEATURES_SERVER_ENDPOINT
from app.db.models import Team
import requests

router = APIRouter(prefix="/lineups", tags=["lineup management"])

@router.post('/generate')
def generate_lineup(req: GenerateLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")

    try:
        team = Team.select().where(
            (Team.user_id == user_id) & (Team.team_id == req.selected_team)
        ).first()
        
        if not team:
            return {"error": "Team not found"}
        
        endpoint = FEATURES_SERVER_ENDPOINT + "/generate-lineup"

        body = {
            "league_id": team.team_info['league_id'], 
            "team_name": team.team_info['team_name'], 
            "espn_s2": team.team_info['espn_s2'], 
            "swid": team.team_info['swid'], 
            "year": team.team_info['year'],
            "threshold": req.threshold,
            "week": req.week
        }
        
        resp = requests.post(endpoint, json=body)
        return resp.json()
        
    except Exception as e:
        print(f"Error in generate_lineup: {e}")
        return {"error": "Failed to generate lineup"}

@router.get('', response_model=GetLineupsResp)
async def get_lineups(team_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await LineupService.get_lineups(user_id, team_id)

@router.put('/save', response_model=SaveLineupResp)
async def save_lineup(req: SaveLineupReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await LineupService.save_lineup(user_id, req.selected_team, req.lineup_info)

@router.delete('/remove', response_model=DeleteLineupResp)
async def remove_lineup(lineup_id: int, current_user: dict = Depends(get_current_user)):
    return await LineupService.remove_lineup(lineup_id)
