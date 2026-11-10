"""
The check-ins' jobs: POST /v1/internal/jobs/checkins/run, one tick of Dime's
schedule, called by data-platform's dime_checkins pipeline every fifteen minutes
(services/ai/checkins.py); and POST /v1/internal/jobs/checkins/events, what came
in about players (a verdict change, a news item), posted by data-platform's
availability and news pipelines (services/ai/events.py). The check-ins they
start run on in the backend; the response says what started, not how it went.
"""

from datetime import date
from typing import Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

INT4_MAX = 2_147_483_647


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


class VerdictChangeIn(ApiModel):
    """A verdict the availability pipeline resolved this tick that changed for a game not yet tipped.
    The backend reads the verdict itself (nba.player_availability)."""

    player_id: int = Field(..., gt=0, le=INT4_MAX)
    game_date: date


class CheckinEventsReq(ApiModel):
    """Body for POST /v1/internal/jobs/checkins/events: keys only, the facts are read here."""

    availability: list[VerdictChangeIn] = Field(default_factory=list, max_length=500)
    news: list[int] = Field(default_factory=list, max_length=500, description="nba.news_items ids just stored")


class CheckinEventsData(ApiModel):
    off: bool                                    # DIME_EVENTS_ENABLED (or AI / autopilot) is off: nothing recorded
    teams: int                                   # switched-on teams on AUTOPILOT_TEAM_IDS
    facts: int                                   # the posted keys that are events at all
    recorded: int                                # new events on a team that rosters the player
    started: list[CheckinStarted]


class CheckinEventsResp(BaseResponse):
    data: Optional[CheckinEventsData] = None
