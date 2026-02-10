"""
Public API routes for NBA schedule information.
"""

from fastapi import APIRouter, Request
from schemas.schedule import ScheduleWeeksResp, ScheduleWeeksData, ScheduleWeek
from schemas.common import ApiStatus
from services.schedule_service import _load_schedule, _parse_date, get_current_matchup
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/schedule", tags=["Schedule"])


@router.get(
    "/weeks",
    response_model=ScheduleWeeksResp,
    summary="Get all schedule weeks",
    description="Returns all NBA schedule weeks with start/end dates and the current week number.",
    responses={
        200: {"description": "Schedule weeks retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_schedule_weeks(request: Request) -> ScheduleWeeksResp:
    """Get all schedule weeks with dates and the current week."""
    schedule = _load_schedule().get("schedule", {})

    weeks = []
    for week_num, week_data in sorted(schedule.items(), key=lambda x: int(x[0])):
        weeks.append(ScheduleWeek(
            week=int(week_num),
            start_date=_parse_date(week_data["startDate"]).isoformat(),
            end_date=_parse_date(week_data["endDate"]).isoformat(),
        ))

    current_matchup = get_current_matchup()
    current_week = current_matchup["matchup_number"] if current_matchup else None

    return ScheduleWeeksResp(
        status=ApiStatus.SUCCESS,
        message="Schedule weeks retrieved successfully",
        data=ScheduleWeeksData(weeks=weeks, current_week=current_week),
    )
