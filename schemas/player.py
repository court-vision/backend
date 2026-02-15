from pydantic import BaseModel
from typing import List, Optional
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
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int


class AvgStats(BaseModel):
    avg_fpts: float
    avg_points: float
    avg_rebounds: float
    avg_assists: float
    avg_steals: float
    avg_blocks: float
    avg_turnovers: float
    avg_minutes: float
    avg_fg_pct: float
    avg_fg3_pct: float
    avg_ft_pct: float
    # Shooting efficiency
    avg_ts_pct: float
    avg_efg_pct: float
    avg_three_rate: float
    avg_ft_rate: float
    # Volume averages
    avg_fgm: float
    avg_fga: float
    avg_fg3m: float
    avg_fg3a: float
    avg_ftm: float
    avg_fta: float


class AdvancedStatsData(BaseModel):
    """Advanced stats from pipeline - always season-level."""
    off_rating: Optional[float] = None
    def_rating: Optional[float] = None
    net_rating: Optional[float] = None
    usg_pct: Optional[float] = None
    ast_pct: Optional[float] = None
    ast_to_tov: Optional[float] = None
    reb_pct: Optional[float] = None
    oreb_pct: Optional[float] = None
    dreb_pct: Optional[float] = None
    tov_pct: Optional[float] = None
    pace: Optional[float] = None
    pie: Optional[float] = None
    plus_minus: Optional[float] = None


class PlayerStats(BaseModel):
    id: int
    name: str
    team: str
    games_played: int
    window: str
    window_games: int
    avg_stats: AvgStats
    advanced_stats: Optional[AdvancedStatsData] = None
    game_logs: List[GameLog]


class PlayerStatsResp(BaseResponse):
    data: PlayerStats | None = None


class PercentileData(BaseModel):
    avg_fpts: int
    avg_points: int
    avg_rebounds: int
    avg_assists: int
    avg_steals: int
    avg_blocks: int
    avg_turnovers: int
    avg_minutes: int
    avg_fg_pct: int
    avg_fg3_pct: int
    avg_ft_pct: int


class PlayerPercentilesResp(BaseResponse):
    data: PercentileData | None = None
