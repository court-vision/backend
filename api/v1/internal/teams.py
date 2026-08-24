import json

from fastapi import APIRouter, Depends
from services.team_service import TeamService
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.team_insights_service import TeamInsightsService
from schemas.team import TeamAddReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from schemas.espn import TeamDataResp
from schemas.team_insights import TeamInsightsResp
from schemas.common import FantasyProvider
from api.deps import get_db_user, get_owned_team
from db.models.users import User
from db.models.teams import Team


router = APIRouter(prefix="/teams", tags=["team management"])


@router.get('/', response_model=TeamGetResp)
async def get_teams(user: User = Depends(get_db_user)):
    return await TeamService.get_teams(user.user_id)

@router.post('/add', response_model=TeamAddResp)
async def add_team(team_add_req: TeamAddReq, user: User = Depends(get_db_user)):
    return await TeamService.add_team(user.user_id, team_add_req.league_info)

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_id: int, user: User = Depends(get_db_user)):
    return await TeamService.remove_team(user.user_id, team_id)

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_update_req: TeamUpdateReq, user: User = Depends(get_db_user)):
    return await TeamService.update_team(user.user_id, team_update_req.team_id, team_update_req.league_info)

@router.get('/view', response_model=TeamDataResp)
async def view_team(team: Team = Depends(get_owned_team)):
    league_info = TeamService.deserialize_league_info(json.loads(team.league_info))

    # Route to correct provider service
    # Pass team_id for Yahoo so tokens can be refreshed and persisted
    if league_info.provider == FantasyProvider.YAHOO:
        return await YahooService.get_team_data(league_info, 0, team.team_id)
    return await EspnService.get_team_data(league_info, 0)


@router.get('/{team_id}/insights', response_model=TeamInsightsResp)
async def get_team_insights(team: Team = Depends(get_owned_team)):
    return await TeamInsightsService.get_team_insights(team.team_id)
