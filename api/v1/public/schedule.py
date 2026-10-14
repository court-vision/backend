"""
Public API routes for NBA schedule information.
"""

from datetime import date

from fastapi import APIRouter, Request
from schemas.schedule import ScheduleWeeksResp, ScheduleWeeksData, ScheduleWeek, SeasonInfo
from schemas.common import ApiStatus
from services.schedule_service import (
    get_current_matchup,
    get_nba_today,
    get_season_bounds,
    get_season_phase,
    iter_weeks,
    season_day,
    seconds_left_in_fantasy_day,
)
from core import public_read_cache as caches
from core.http_cache import serve_cached_envelope
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
    # The static calendar plus today's fantasy day (2 AM ET rule): one entry per
    # day, built from that day, and never held by a client past the rollover.
    today = get_nba_today()

    async def build() -> ScheduleWeeksResp:
        return _schedule_weeks(today)

    return await serve_cached_envelope(
        request, caches.SCHEDULE_WEEKS, caches.key(today), build,
        max_age=seconds_left_in_fantasy_day(today),
    )


def _schedule_weeks(today: date) -> ScheduleWeeksResp:
    weeks = [
        ScheduleWeek(
            week=w["matchup_number"],
            start_date=w["start_date"].isoformat(),
            end_date=w["end_date"].isoformat(),
            game_span=w["game_span"],
        )
        for w in iter_weeks()
    ]

    current_matchup = get_current_matchup(today)
    current_week = current_matchup["matchup_number"] if current_matchup else None

    bounds = get_season_bounds()
    season = SeasonInfo(
        key=bounds.season,
        label=season_label(bounds.season),
        espn_year=bounds.espn_year,
        preseason_start=bounds.preseason_start.isoformat() if bounds.preseason_start else None,
        regular_season_start=bounds.opening_night.isoformat(),
        regular_season_end=bounds.regular_season_end.isoformat(),
        phase=get_season_phase(today),
        season_day=season_day(today),
        week_count=bounds.week_count,
    )

    return ScheduleWeeksResp(
        status=ApiStatus.SUCCESS,
        message="Schedule weeks retrieved successfully",
        data=ScheduleWeeksData(weeks=weeks, current_week=current_week, season=season),
    )
