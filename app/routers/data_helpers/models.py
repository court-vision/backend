from pydantic import BaseModel
from typing import Optional



# -------------------------- Team Validation Models ------------------------- #

class ValidateLeagueResp(BaseModel):
    valid: bool
    message: str

# -------------------- Team/League Data Retrieval Models -------------------- #

class LeagueInfo(BaseModel):
    league_id: int
    espn_s2: Optional[str]
    swid: Optional[str]
    team_name: str
    year: int

class TeamDataReq(BaseModel):
    league_info: LeagueInfo
    fa_count: int

class PlayerResp(BaseModel):
    name: str
    avg_points: float
    team: str
    valid_positions: list[str]
    injured: bool