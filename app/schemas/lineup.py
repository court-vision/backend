from pydantic import BaseModel, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, LineupResponse

# ------------------------------- Lineup Models ------------------------------- #

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
    team_id: int = Field(ge=1, description="Team ID must be positive")
    threshold: float = Field(ge=0, description="Threshold must be non-negative")
    week: int = Field(ge=1, le=20, description="Week must be between 1 and 20")

class SaveLineupReq(BaseRequest):
    team_id: int = Field(ge=1, description="Team ID must be positive")
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
