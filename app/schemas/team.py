from pydantic import BaseModel, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, TeamResponse

# ------------------------------- Team Management Models ------------------------------- #

class LeagueInfo(BaseModel):
    league_id: int = Field(ge=1, description="League ID must be positive")
    espn_s2: str | None = ""
    swid: str | None = ""
    team_name: str = Field(min_length=1, description="Team name cannot be empty")
    league_name: str | None = "N/A"
    year: int = Field(ge=2020, le=2030, description="Year must be between 2020 and 2030")

#                          ------- Incoming -------                           #

class TeamAddReq(BaseRequest):
    league_info: LeagueInfo

class TeamRemoveReq(BaseRequest):
    team_id: int = Field(ge=1, description="Team ID must be positive")
  
class TeamUpdateReq(BaseRequest):
    team_id: int = Field(ge=1, description="Team ID must be positive")
    league_info: LeagueInfo

#                          ------- Outgoing -------                           #

class TeamGetResp(BaseResponse):
    """Get user teams response"""
    data: Optional[list[TeamResponse]] = None

class TeamAddResp(BaseResponse):
    """Add team response"""
    data: Optional[TeamResponse] = None

class TeamRemoveResp(BaseResponse):
    """Remove team response"""
    data: Optional[dict] = None

class TeamUpdateResp(BaseResponse):
    """Update team response"""
    data: Optional[TeamResponse] = None
