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
    access_token: str
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
    espn_s2: Optional[str]
    swid: Optional[str]
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
    team_id: int
    already_exists: bool

class TeamRemoveResp(BaseModel):
    success: bool

class TeamUpdateResp(BaseModel):
    success: bool