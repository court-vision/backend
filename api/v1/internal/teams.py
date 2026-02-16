from fastapi import APIRouter, Depends
from services.team_service import TeamService
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.user_sync_service import UserSyncService
from services.team_insights_service import TeamInsightsService
from schemas.team import TeamAddReq, TeamRemoveReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from schemas.espn import TeamDataReq, TeamDataResp
from schemas.team_insights import TeamInsightsResp
from schemas.common import ApiStatus, FantasyProvider
from core.clerk_auth import get_current_user


router = APIRouter(prefix="/teams", tags=["team management"])


def _get_user_id(current_user: dict) -> int:
    """Helper to get local user_id from Clerk user info."""
    clerk_user_id = current_user.get("clerk_user_id")
    email = current_user.get("email")
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return user.user_id


@router.get('/', response_model=TeamGetResp)
async def get_teams(current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await TeamService.get_teams(user_id)

@router.post('/add', response_model=TeamAddResp)
async def add_team(team_add_req: TeamAddReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await TeamService.add_team(user_id, team_add_req.league_info)

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_id: int, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await TeamService.remove_team(user_id, team_id)

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_update_req: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    return await TeamService.update_team(user_id, team_update_req.team_id, team_update_req.league_info)

@router.get('/view', response_model=TeamDataResp)
async def view_team(team_id: int, _: dict = Depends(get_current_user)):
    team_view_resp = await TeamService.view_team(team_id)
    if team_view_resp.status != ApiStatus.SUCCESS:
        return TeamDataResp(status=ApiStatus.ERROR, message="Failed to fetch team data", data=None)

    league_info = team_view_resp.data.league_info

    # Route to correct provider service
    # Pass team_id for Yahoo so tokens can be refreshed and persisted
    if league_info.provider == FantasyProvider.YAHOO:
        return await YahooService.get_team_data(league_info, 0, team_id)
    return await EspnService.get_team_data(league_info, 0)


@router.get('/{team_id}/insights', response_model=TeamInsightsResp)
async def get_team_insights(team_id: int, _: dict = Depends(get_current_user)):
    return await TeamInsightsService.get_team_insights(team_id)
