from fastapi import APIRouter, Depends
from app.services.team_service import TeamService
from app.services.espn_service import EspnService
from app.schemas.team import TeamAddReq, TeamRemoveReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from app.schemas.espn import TeamDataReq
from app.core.security import get_current_user
from app.utils.constants import SELF_ENDPOINT
import httpx

router = APIRouter(prefix="/teams", tags=["team management"])

@router.get('/', response_model=TeamGetResp)
async def get_teams(current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.get_teams(user_id)

@router.post('/add', response_model=TeamAddResp)
async def add_team(team_info: TeamAddReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.add_team(user_id, team_info)

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_info: TeamRemoveReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.remove_team(user_id, team_info.team_id)

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_info: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.update_team(user_id, team_info.team_id, team_info)

@router.get('/view')
async def view_team(team_id: int):
    team_result = await TeamService.view_team(team_id)
    if "error" in team_result:
        return team_result
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{SELF_ENDPOINT}/api/v1/internal/espn/get_roster_data", json={"league_info": team_result["team_info"], "fa_count": 0})
        
        return resp.json()
        
    except Exception as e:
        print(f"Error in view_team: {e}")
        return {"error": "Failed to fetch team data"}
