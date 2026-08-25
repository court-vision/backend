"""
Public API routes for NBA schedule information.
"""

from fastapi import APIRouter, Request
from schemas.schedule import ScheduleWeeksResp, ScheduleWeeksData, ScheduleWeek, SeasonInfo
from schemas.common import ApiStatus
from services.schedule_service import (
    get_current_matchup,
    get_season_bounds,
    get_season_phase,
    iter_weeks,
    season_day,
)
from core.season import season_label
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/schedule", tags=["Schedule"])


@router.get(
    "/weeks",
    response_model=ScheduleWeeksResp,
    summary="Get all schedule weeks",
    description=(
        "Returns every fantasy week of the active season with start/end dates, "
        "the current week number, and a `season` block (key, dates, phase)."
    ),
    responses={
        200: {"description": "Schedule weeks retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_schedule_weeks(request: Request) -> ScheduleWeeksResp:
    """Get all schedule weeks with dates, the current week, and season info."""
    weeks = [
        ScheduleWeek(
            week=w["matchup_number"],
            start_date=w["start_date"].isoformat(),
            end_date=w["end_date"].isoformat(),
            game_span=w["game_span"],
        )
        for w in iter_weeks()
    ]

    current_matchup = get_current_matchup()
    current_week = current_matchup["matchup_number"] if current_matchup else None

    bounds = get_season_bounds()
    season = SeasonInfo(
        key=bounds.season,
        label=season_label(bounds.season),
        espn_year=bounds.espn_year,
        preseason_start=bounds.preseason_start.isoformat() if bounds.preseason_start else None,
        regular_season_start=bounds.opening_night.isoformat(),
        regular_season_end=bounds.regular_season_end.isoformat(),
        phase=get_season_phase(),
        season_day=season_day(),
        week_count=bounds.week_count,
    )

    return ScheduleWeeksResp(
        status=ApiStatus.SUCCESS,
        message="Schedule weeks retrieved successfully",
        data=ScheduleWeeksData(weeks=weeks, current_week=current_week, season=season),
    )
