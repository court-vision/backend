from pydantic import Field
from typing import Optional
from .common import BaseRequest, BaseResponse, TeamResponse, LeagueInfoWrite

# ------------------------------- Team Management Models ------------------------------- #

#                          ------- Incoming -------                           #

class TeamAddReq(BaseRequest):
    league_info: LeagueInfoWrite

class TeamRemoveReq(BaseRequest):
    team_id: int = Field(ge=1, description="Team ID must be positive")
  
class TeamUpdateReq(BaseRequest):
    team_id: int = Field(ge=1, description="Team ID must be positive")
    league_info: LeagueInfoWrite

#                          ------- Outgoing -------                           #

class TeamGetResp(BaseResponse):
    """Get user teams response"""
    data: Optional[list[TeamResponse]] = None

class TeamAddResp(BaseResponse):
    """Add team response"""
    data: Optional[TeamResponse] = None
    team_id: Optional[int] = None
    already_exists: bool = False

class TeamRemoveResp(BaseResponse):
    """Remove team response"""
    data: Optional[int] = None

class TeamUpdateResp(BaseResponse):
    """Update team response"""
    data: Optional[TeamResponse] = None

class TeamViewResp(BaseResponse):
    """View team response"""
    data: Optional[TeamResponse] = None