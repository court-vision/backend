from pydantic import BaseModel
from typing import Optional


# ------------------------------- User Models ------------------------------- #

#                          ------- Incoming -------                           #

class UserCreateReq(BaseModel):
    email: str
    password: str

class UserLoginReq(BaseModel):
    email: str
    password: str

class UserUpdateReq(BaseModel):
    email: Optional[str]
    password: Optional[str]

#                          ------- Outgoing -------                           #

class UserCreateResp(BaseModel):
    access_token: str | None
    already_exists: bool

class UserLoginResp(BaseModel):
    access_token: str
    success: bool

class UserDeleteResp(BaseModel):
    success: bool

class UserUpdateResp(BaseModel):
    success: bool

# -------------------------- Team Management Models ------------------------- #

class LeagueInfo(BaseModel):
    league_id: int
    espn_s2: str | None = ""
    swid: str | None = ""
    team_name: str
    year: int

#                          ------- Incoming -------                           #

class TeamAddReq(BaseModel):
    league_info: LeagueInfo

class TeamRemoveReq(BaseModel):
    team_id: int
  
class TeamUpdateReq(BaseModel):
    team_id: int
    league_info: LeagueInfo

#                          ------- Outgoing -------                           #

class TeamGetResp(BaseModel):
    teams: list[dict]

class TeamAddResp(BaseModel):
    team_id: int | None
    already_exists: bool

class TeamRemoveResp(BaseModel):
    success: bool

class TeamUpdateResp(BaseModel):
    success: bool

# ------------------------------ Lineup Models ------------------------------ #

#                          ------ Sub-Models ------                           #

class SlimPlayer(BaseModel):
    Name: str
    AvgPoints: float
    Team: str

class SlimGene(BaseModel):
    Day: int
    Additions: list[SlimPlayer]
    Removals: list[SlimPlayer]
    Roster: dict[str, SlimPlayer]

class LineupInfo(BaseModel):
    Lineup: list[SlimGene]
    Improvement: int
    Timestamp: str
    Id: int | None = None

#                          ------- Incoming -------                           #

class GenerateLineupReq(BaseModel):
    selected_team: int
    threshold: str
    week: str

class SaveLineupReq(BaseModel):
    selected_team: int
    lineup_info: LineupInfo


#                          ------- Outgoing -------                           #

class GenerateLineupResp(BaseModel):
    league_id: int
    espn_s2: str
    swid: str
    team_name: str
    year: int
    threshold: float
    week: str

class GetLineupsResp(BaseModel):
    lineups: list[LineupInfo] | None
    no_lineups: bool

class SaveLineupResp(BaseModel):
    success: bool
    already_exists: bool