from typing import Optional

from fastapi import APIRouter, Depends, Query
from services.team_service import TeamService
from services.providers import get_provider_adapter
from services.team_insights_service import TeamInsightsService
from schemas.team import TeamAddReq, TeamUpdateReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from schemas.espn import TeamDataResp
from schemas.team_insights import TeamInsightsResp
from schemas.common import ApiStatus, FantasyProvider
from schemas.league import LeagueGetResp, LeagueSyncResp
from schemas.league_projection import LeagueProjectionResp
from schemas.league_values import LeagueValuesResp, ValueWindowKey
from schemas.team_overview import TeamsOverviewResp
from services.league_service import LeagueService
from services.league_projection_service import league_projection
from services.league_values_service import FREE_AGENTS_READ, FREE_AGENTS_SHOWN, league_values
from services import team_overview_service
from api.deps import UserContext, OwnedTeamContext, get_db_user, get_owned_team, load_owned_league_info
from core.responses import respond


router = APIRouter(prefix="/teams", tags=["team management"])


@router.get('/', response_model=TeamGetResp)
async def get_teams(user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.get_teams(user.user_id))

@router.get('/overview', response_model=TeamsOverviewResp)
async def get_teams_overview(user: UserContext = Depends(get_db_user)):
    """Every team of the account at a glance: each one's state (needs you, your call, set,
    autopilot, can't read) with the day's actions behind it, tonight's lineup, the matchup and
    its week, the season line, and what is scheduled next. One team that can't be read never
    fails the others. Never writes."""
    data = await team_overview_service.overview(user.user_id)
    return respond(TeamsOverviewResp(status=ApiStatus.SUCCESS, message=f"{len(data.teams)} team(s)", data=data))


@router.post('/add', response_model=TeamAddResp)
async def add_team(team_add_req: TeamAddReq, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.add_team(user.user_id, team_add_req.league_info.to_internal()))

@router.delete('/remove', response_model=TeamRemoveResp)
async def remove_team(team_id: int, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.remove_team(user.user_id, team_id))

@router.put('/update', response_model=TeamUpdateResp)
async def update_team(team_update_req: TeamUpdateReq, user: UserContext = Depends(get_db_user)):
    return respond(await TeamService.update_team(user.user_id, team_update_req.team_id, team_update_req.league_info.to_internal()))

@router.get('/view', response_model=TeamDataResp)
async def view_team(team: OwnedTeamContext = Depends(get_owned_team)):
    league_info = await load_owned_league_info(team)

    adapter = get_provider_adapter(league_info.provider)
    return respond(await adapter.get_team(league_info, team_id=team.team_id))


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


@router.get('/{team_id}/league/projections', response_model=LeagueProjectionResp)
async def get_team_league_projections(team: OwnedTeamContext = Depends(get_owned_team)):
    """Every roster in the team's league projected under the league's scoring, the league's
    projections as the team's benchmark, and playoff odds (ESPN points leagues)."""
    league_info = await load_owned_league_info(team)
    data = await league_projection(league_info)
    return respond(LeagueProjectionResp(status=ApiStatus.SUCCESS, message="League projected", data=data))


@router.get('/{team_id}/league/values', response_model=LeagueValuesResp)
async def get_team_league_values(
    team: OwnedTeamContext = Depends(get_owned_team),
    window: Optional[ValueWindowKey] = Query(None, description="A preset window; this_week when no matchups are given"),
    first_matchup: Optional[int] = Query(None, ge=1, description="The window's first matchup (wins over the preset)"),
    last_matchup: Optional[int] = Query(None, ge=1, description="The window's last matchup"),
    free_agents: int = Query(FREE_AGENTS_SHOWN, ge=0, le=FREE_AGENTS_READ, description="Free agents to return"),
):
    """Every rostered player in the team's league and its best free agents priced over a
    window of matchups under the league's scoring: expected games and points, what each
    adds to his roster's lineup, ranks for the window and the rest of the season, and each
    roster's tiers against the league's (ESPN points leagues)."""
    league_info = await load_owned_league_info(team)
    data = await league_values(league_info, window=window, first_matchup=first_matchup,
                               last_matchup=last_matchup, free_agents=free_agents)
    return respond(LeagueValuesResp(status=ApiStatus.SUCCESS, message="League priced", data=data))


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
