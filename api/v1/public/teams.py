"""
Public API routes for team information.
"""

from fastapi import APIRouter, Request, Path, Query
from schemas.teams import TeamScheduleResp, NBATeamStatsResp, NBATeamRosterResp, NBATeamLiveGameResp
from services.team_schedule_service import TeamScheduleService
from services.nba_team_stats_service import NBATeamStatsService
from services.nba_team_roster_service import NBATeamRosterService
from services.nba_team_live_game_service import NBATeamLiveGameService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/teams", tags=["Teams"])


@router.get(
    "/{team_abbrev}/schedule",
    response_model=TeamScheduleResp,
    summary="Get team schedule",
    description="Returns the schedule for an NBA team including upcoming and past games.",
    responses={
        200: {"description": "Schedule retrieved successfully"},
        404: {"description": "Team not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_team_schedule(
    request: Request,
    team_abbrev: str = Path(..., description="Team abbreviation (e.g., LAL, BOS, GSW)"),
    upcoming: bool = Query(False, description="Only return future games"),
    limit: int = Query(20, ge=1, le=200, description="Maximum number of games to return"),
) -> TeamScheduleResp:
    """Get schedule for a team."""
    return await TeamScheduleService.get_team_schedule(
        team_abbrev=team_abbrev,
        upcoming=upcoming,
        limit=limit,
    )


@router.get(
    "/{team_abbrev}/stats",
    response_model=NBATeamStatsResp,
    summary="Get team season stats",
    description="Returns the latest season-to-date statistics for an NBA team.",
    responses={
        200: {"description": "Stats retrieved successfully"},
        404: {"description": "Team not found or no stats available"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_team_stats(
    request: Request,
    team_abbrev: str = Path(..., description="Team abbreviation (e.g., LAL, BOS, GSW)"),
) -> NBATeamStatsResp:
    """Get season stats for a team."""
    return await NBATeamStatsService.get_team_stats(team_abbrev=team_abbrev)


@router.get(
    "/{team_abbrev}/roster",
    response_model=NBATeamRosterResp,
    summary="Get team roster",
    description="Returns the active roster for an NBA team with per-game averages and injury status.",
    responses={
        200: {"description": "Roster retrieved successfully"},
        404: {"description": "Team not found or no data available"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_team_roster(
    request: Request,
    team_abbrev: str = Path(..., description="Team abbreviation (e.g., LAL, BOS, GSW)"),
) -> NBATeamRosterResp:
    """Get active roster for a team."""
    return await NBATeamRosterService.get_team_roster(team_abbrev=team_abbrev)


@router.get(
    "/{team_abbrev}/live-game",
    response_model=NBATeamLiveGameResp,
    summary="Get team live/upcoming game",
    description="Returns the live, most recent, or next upcoming game for an NBA team.",
    responses={
        200: {"description": "Game data retrieved successfully"},
        404: {"description": "Team not found or no games available"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_team_live_game(
    request: Request,
    team_abbrev: str = Path(..., description="Team abbreviation (e.g., LAL, BOS, GSW)"),
) -> NBATeamLiveGameResp:
    """Get live or upcoming game for a team."""
    return await NBATeamLiveGameService.get_live_game(team_abbrev=team_abbrev)
