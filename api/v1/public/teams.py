"""
Public API routes for team information.
"""

from fastapi import APIRouter, Request, Path, Query
from schemas.teams import TeamScheduleResp
from services.team_schedule_service import TeamScheduleService
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
    limit: int = Query(20, ge=1, le=100, description="Maximum number of games to return"),
) -> TeamScheduleResp:
    """Get schedule for a team."""
    return await TeamScheduleService.get_team_schedule(
        team_abbrev=team_abbrev,
        upcoming=upcoming,
        limit=limit,
    )
