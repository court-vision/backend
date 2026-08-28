from fastapi import APIRouter, Depends
from services.team_service import TeamService
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.team_insights_service import TeamInsightsService
from schemas.team import TeamAddReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from schemas.espn import TeamDataResp
from schemas.team_insights import TeamInsightsResp
from schemas.common import ApiStatus, FantasyProvider
from schemas.league import LeagueGetResp, LeagueSyncResp
from services.league_service import LeagueService
from api.deps import UserContext, OwnedTeamContext, get_db_user, get_owned_team, load_owned_league_info
from core.responses import respond


router = APIRouter(prefix="/teams", tags=["team management"])


@router.get('/', response_model=TeamGetResp)
async def get_teams(user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.get_teams(user.user_id))

@router.post('/add', response_model=TeamAddResp)
async def add_team(team_add_req: TeamAddReq, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.add_team(user.user_id, team_add_req.league_info))

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_id: int, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.remove_team(user.user_id, team_id))

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_update_req: TeamUpdateReq, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.update_team(user.user_id, team_update_req.team_id, team_update_req.league_info))

@router.get('/view', response_model=TeamDataResp)
async def view_team(team: OwnedTeamContext = Depends(get_owned_team)):
    league_info = await load_owned_league_info(team)

    # Route to correct provider service
    # Pass team_id for Yahoo so tokens can be refreshed and persisted
    if league_info.provider == FantasyProvider.YAHOO:
        return respond(await YahooService.get_team_data(league_info, 0, team.team_id))
    return respond(await EspnService.get_team_data(league_info, 0))


@router.get('/{team_id}/insights', response_model=TeamInsightsResp)
async def get_team_insights(team: OwnedTeamContext = Depends(get_owned_team)):
    return await TeamInsightsService.get_team_insights(team.team_id)


@router.get('/{team_id}/league', response_model=LeagueGetResp)
async def get_team_league(team: OwnedTeamContext = Depends(get_owned_team)):
    """Provider-detected league settings for an owned team (None until synced)."""
    if team.league_id is None:
        return LeagueGetResp(status=ApiStatus.SUCCESS, message="League settings not synced yet", data=None)
    return LeagueGetResp(
        status=ApiStatus.SUCCESS, message="League fetched",
        data=team.league,
    )


@router.post('/{team_id}/league/sync', response_model=LeagueSyncResp)
async def sync_team_league(team: OwnedTeamContext = Depends(get_owned_team)):
    """Re-fetch the league's scoring settings from the provider and store them.

    Deliberately not wrapped in `respond`: an ERROR envelope *with data* here is a
    soft warning ("provider settings unavailable; using default points scoring")
    that the client renders, so it stays a 200.
    """
    league_info = await load_owned_league_info(team)
    league = await LeagueService.sync_for_team(team.team_id, league_info)
    if league is None:
        return LeagueSyncResp(status=ApiStatus.ERROR, message="Could not sync league settings", data=None)
    synced = league.settings_synced
    return LeagueSyncResp(
        status=ApiStatus.SUCCESS if synced else ApiStatus.ERROR,
        message="League settings synced" if synced else "Provider settings unavailable; using default points scoring",
        data=league,
    )
