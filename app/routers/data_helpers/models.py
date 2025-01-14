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

# ------------------------------- ETL Models ------------------------------- #

# Can modify this later to contain the time-series data, for now just the average and total FPTS
class FPTSPlayer(BaseModel):
    rank: int
    player_id: int
    player_name: str
    total_fpts: float
    avg_fpts: float
    rank_change: int | None = None

#                          ------- Incoming -------                           #

class ETLUpdateFTPSReq(BaseModel):
    cron_token: str

#                          ------- Outgoing -------                           #
class ETLUpdateFTPSResp(BaseModel):
    success: bool
    data: list[FPTSPlayer] | None