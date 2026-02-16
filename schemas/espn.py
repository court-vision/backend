from pydantic import BaseModel
from typing import Optional
from .common import BaseRequest, BaseResponse, LeagueInfo

# ------------------------------- ESPN Data Models ------------------------------- #

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

class TeamDataReq(BaseRequest):
    league_info: LeagueInfo
    fa_count: int

class ValidateLeagueResp(BaseResponse):
    valid: bool
    message: str

class TeamDataResp(BaseResponse):
    data: Optional[list[PlayerResp]] = None