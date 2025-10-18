from pydantic import BaseModel
from typing import Optional
from .common import BaseRequest, BaseResponse, LeagueInfo

# ------------------------------- ESPN Data Models ------------------------------- #

class ValidateLeagueReq(BaseRequest):
    league_info: LeagueInfo

class PlayerResp(BaseModel):
    name: str
    avg_points: float
    team: str
    valid_positions: list[str]
    injured: bool

class TeamDataReq(BaseRequest):
    league_info: LeagueInfo
    fa_count: int

class ValidateLeagueResp(BaseResponse):
    valid: bool
    message: str

class TeamDataResp(BaseResponse):
    data: Optional[list[PlayerResp]] = None