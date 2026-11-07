"""
The check-ins' job (POST /v1/internal/jobs/checkins/run): one tick of Dime's
schedule, called by data-platform's dime_checkins pipeline every fifteen minutes
(services/ai/checkins.py). The check-ins it starts run on in the backend; the
response says what it started, not how they went.
"""

from datetime import date
from typing import Optional

from schemas.common import ApiModel, BaseResponse


class CheckinStarted(ApiModel):
    checkin_id: int
    team_id: int
    kind: str
    date: date


class CheckinRunData(ApiModel):
    off: bool                                    # AI_ENABLED or AUTOPILOT_ENABLED is off: nothing ran
    teams: int                                   # switched-on teams on AUTOPILOT_TEAM_IDS
    started: list[CheckinStarted]
    missed: int                                  # windows that closed before their check-in could run
    skipped: int                                 # refused: the monthly budget, the day's cap
    stale: int                                   # running check-ins whose process died, closed as failed


class CheckinRunResp(BaseResponse):
    data: Optional[CheckinRunData] = None
