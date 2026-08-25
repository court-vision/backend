from pydantic import BaseModel, Field
from typing import Literal, Optional
from .common import BaseRequest, BaseResponse, LeagueInfo

# ------------------------------- ESPN Data Models ------------------------------- #

# What `avg_points` measures: fantasy points under the league's weights, or the
# fpts-scale category value proxy for H2H-category leagues.
ValueKind = Literal["fpts", "cat_value"]

class ValidateLeagueReq(BaseRequest):
    league_info: LeagueInfo

class PlayerResp(BaseModel):
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