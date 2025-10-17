from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from ..base_models import BaseRequest, BaseResponse, AuthResponse, VerificationResponse, UserResponse, TeamResponse, LineupResponse

# ------------------------------- User Models ------------------------------- #

#                          ------- Incoming -------                           #

class UserCreateReq(BaseRequest):
    email: EmailStr
    password: str = Field(min_length=8, description="Password must be at least 8 characters")

class UserLoginReq(BaseRequest):
    email: EmailStr
    password: str

class UserUpdateReq(BaseRequest):
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=8, description="Password must be at least 8 characters")

class UserDeleteReq(BaseRequest):
    password: str

class VerifyEmailReq(BaseRequest):
    email: str
    password: str

class CheckCodeReq(BaseRequest):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, description="Verification code must be 6 digits")

#                          ------- Outgoing -------                           #

class UserCreateResp(BaseResponse):
    """User creation response with authentication data"""
    data: Optional[AuthResponse] = None

class UserLoginResp(BaseResponse):
    """User login response with authentication data"""
    data: Optional[AuthResponse] = None

class UserDeleteResp(BaseResponse):
    """User deletion response"""
    data: Optional[dict] = None

class UserUpdateResp(BaseResponse):
    """User update response"""
    data: Optional[UserResponse] = None

class VerifyEmailResp(BaseResponse):
    """Email verification request response"""
    data: Optional[VerificationResponse] = None

class CheckCodeResp(BaseResponse):
    """Email verification code check response"""
    data: Optional[AuthResponse] = None

# -------------------------- Team Management Models ------------------------- #

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

# ------------------------------ Lineup Models ------------------------------ #

#                          ------ Sub-Models ------                           #

class SlimPlayer(BaseModel):
    Name: str = Field(min_length=1, description="Player name cannot be empty")
    AvgPoints: float = Field(ge=0, description="Average points must be non-negative")
    Team: str = Field(min_length=1, description="Team name cannot be empty")

class SlimGene(BaseModel):
    Day: int = Field(ge=1, le=7, description="Day must be between 1 and 7")
    Additions: list[SlimPlayer] = Field(default_factory=list)
    Removals: list[SlimPlayer] = Field(default_factory=list)
    Roster: dict[str, SlimPlayer] = Field(default_factory=dict)

class LineupInfo(BaseModel):
    Lineup: list[SlimGene] = Field(min_items=1, description="Lineup must have at least one gene")
    Improvement: int = Field(description="Improvement value")
    Timestamp: str = Field(description="Lineup timestamp")
    Week: str = Field(min_length=1, description="Week cannot be empty")
    Threshold: float = Field(ge=0, le=1, description="Threshold must be between 0 and 1")
    Id: int | None = None

#                          ------- Incoming -------                           #

class GenerateLineupReq(BaseRequest):
    selected_team: int = Field(ge=1, description="Team ID must be positive")
    threshold: str = Field(pattern=r'^0\.\d+$', description="Threshold must be a decimal between 0 and 1")
    week: str = Field(min_length=1, description="Week cannot be empty")

class SaveLineupReq(BaseRequest):
    selected_team: int = Field(ge=1, description="Team ID must be positive")
    lineup_info: LineupInfo

#                          ------- Outgoing -------                           #

class GenerateLineupResp(BaseResponse):
    """Generate lineup response"""
    data: Optional[LineupInfo] = None

class GetLineupsResp(BaseResponse):
    """Get user lineups response"""
    data: Optional[list[LineupInfo]] = None

class SaveLineupResp(BaseResponse):
    """Save lineup response"""
    data: Optional[LineupResponse] = None

class DeleteLineupResp(BaseResponse):
    """Delete lineup response"""
    data: Optional[dict] = None