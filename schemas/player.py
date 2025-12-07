from pydantic import BaseModel
from typing import List
from .common import BaseResponse


class GameLog(BaseModel):
    date: str
    fpts: int
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    min: int


class AvgStats(BaseModel):
    avg_fpts: float
    avg_points: float
    avg_rebounds: float
    avg_assists: float
    avg_steals: float
    avg_blocks: float
    avg_turnovers: float
    avg_minutes: float


class PlayerStats(BaseModel):
    id: int
    name: str
    team: str
    games_played: int
    avg_stats: AvgStats
    game_logs: List[GameLog]


class PlayerStatsResp(BaseResponse):
    data: PlayerStats | None = None

