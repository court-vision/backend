from fastapi import APIRouter, Depends
from app.services.team_service import TeamService
from app.services.espn_service import EspnService
from app.schemas.team import TeamAddReq, TeamRemoveReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from app.schemas.espn import TeamDataReq, TeamDataResp
from app.schemas.common import ApiStatus
from app.core.security import get_current_user
from app.utils.constants import SELF_ENDPOINT
import httpx


router = APIRouter(prefix="/teams", tags=["team management"])

@router.get('/', response_model=TeamGetResp)
async def get_teams(current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.get_teams(user_id)

@router.post('/add', response_model=TeamAddResp)
async def add_team(team_add_req: TeamAddReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.add_team(user_id, team_add_req.league_info)

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.remove_team(user_id, team_id)

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_update_req: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await TeamService.update_team(user_id, team_update_req.team_id, team_update_req.league_info)

@router.get('/view', response_model=TeamDataResp)
async def view_team(team_id: int, _: dict = Depends(get_current_user)):
    team_view_resp = await TeamService.view_team(team_id)
    if team_view_resp.status != ApiStatus.SUCCESS:
        return TeamDataResp(status=ApiStatus.ERROR, message="Failed to fetch team data", data=None)
    
    league_info = team_view_resp.data.league_info
    return await EspnService.get_team_data(league_info, 0)
