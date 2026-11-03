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
    # The season the team was on when this add moved it to the requested one
    # (the same team on ESPN, saved for an earlier season); None otherwise
    moved_from_season: Optional[int] = None

class TeamRemoveResp(BaseResponse):
    """Remove team response"""
    data: Optional[int] = None

class TeamUpdateResp(BaseResponse):
    """Update team response"""
    data: Optional[TeamResponse] = None

class TeamViewResp(BaseResponse):
    """View team response"""
    data: Optional[TeamResponse] = None