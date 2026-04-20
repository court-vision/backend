from pydantic import BaseModel
from typing import Optional
from .common import BaseResponse


class PlayoffSeriesResp(BaseModel):
    series_id: str
    conference: str           # "East", "West", "Finals"
    round_num: int            # 1-4
    top_seed_team_id: Optional[int]
    top_seed_name: Optional[str]
    top_seed_abbr: str
    top_seed_wins: int
    bottom_seed_team_id: Optional[int]
    bottom_seed_name: Optional[str]
    bottom_seed_abbr: str
    bottom_seed_wins: int
    series_complete: bool
    series_leader_abbr: Optional[str]
    updated_at: Optional[str]


class PlayoffRound(BaseModel):
    round_num: int
    round_name: str           # "First Round", "Conference Semifinals", etc.
    series: list[PlayoffSeriesResp]


class PlayoffBracketData(BaseModel):
    season: str               # "2025-26"
    rounds: list[PlayoffRound]


class PlayoffBracketResp(BaseResponse):
    data: Optional[PlayoffBracketData] = None
