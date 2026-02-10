from pydantic import BaseModel
from typing import Optional
from .common import BaseResponse


class ScheduleWeek(BaseModel):
    week: int
    start_date: str
    end_date: str


class ScheduleWeeksData(BaseModel):
    weeks: list[ScheduleWeek]
    current_week: Optional[int] = None


class ScheduleWeeksResp(BaseResponse):
    data: Optional[ScheduleWeeksData] = None
