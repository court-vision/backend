from pydantic import Field
from typing import Optional
from .common import BaseRequest, BaseResponse, TeamResponse, LeagueInfo

# ------------------------------- Team Management Models ------------------------------- #

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
    data: Optional[int] = None

class TeamUpdateResp(BaseResponse):
    """Update team response"""
    data: Optional[TeamResponse] = None

class TeamViewResp(BaseResponse):
    """View team response"""
    data: Optional[TeamResponse] = None