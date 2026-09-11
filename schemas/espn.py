from datetime import date

from pydantic import BaseModel, Field
from typing import Literal, Optional
from .common import ApiModel, BaseRequest, BaseResponse, LeagueInfo

# ------------------------------- ESPN Data Models ------------------------------- #

# What `avg_points` measures: fantasy points under the league's weights, or the
# fpts-scale category value proxy for H2H-category leagues.
ValueKind = Literal["fpts", "cat_value"]
# ESPN's player-pool status for someone not on a roster: claimable now, or on
# waivers until the league's next waiver run. None for rostered players and
# for providers that do not report one.
AcquisitionStatus = Literal["free_agent", "waivers"]

class ValidateLeagueReq(BaseRequest):
    league_info: LeagueInfo

class PlayerResp(ApiModel):
    player_id: int
    name: str
    avg_points: float
    team: str
    valid_positions: list[str]
    injured: bool
    injury_status: Optional[str] = None
    value_kind: ValueKind = "fpts"
    # Where avg_points came from: rolling | recent | baseline (last season) | provider (ESPN's own number)
    value_source: Optional[str] = None
    acquisition_status: Optional[AcquisitionStatus] = None
    waivers_until: Optional[date] = None   # the day the waiver claim window closes (ESPN's waiverProcessDate)
    default_position_id: Optional[int] = None   # ESPN's defaultPositionId (1 PG … 5 C); None for Yahoo

class TeamDataReq(BaseRequest):
    league_info: LeagueInfo
    fa_count: int

class ValidateLeagueResp(BaseResponse):
    valid: bool
    message: str
    # Raw provider league payload from validation; reused for league settings sync.
    # Excluded from serialization so API responses are unchanged.
    league_payload: Optional[dict] = Field(default=None, exclude=True)

class TeamDataResp(BaseResponse):
    data: Optional[list[PlayerResp]] = None