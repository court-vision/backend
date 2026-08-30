from pydantic import BaseModel
from typing import Literal, Optional
from .common import ApiModel, BaseResponse


class ScheduleWeek(ApiModel):
    week: int
    start_date: str
    end_date: str
    game_span: int


class SeasonInfo(ApiModel):
    """The active season as the calendar sees it (frontend copy/phase come from here)."""
    key: str                                   # "2026-27"
    label: str                                 # "2026–27"
    espn_year: int                             # 2027
    preseason_start: Optional[str] = None      # ISO date or null
    regular_season_start: str                  # ISO date (opening night)
    regular_season_end: str                    # ISO date (last fantasy day)
    phase: Literal["preseason", "regular", "offseason"]
    season_day: Optional[int] = None           # 1-based day of the regular season, null outside it
    week_count: int


class ScheduleWeeksData(ApiModel):
    weeks: list[ScheduleWeek]
    current_week: Optional[int] = None
    season: SeasonInfo


class ScheduleWeeksResp(BaseResponse):
    data: Optional[ScheduleWeeksData] = None
