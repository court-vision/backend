from pydantic import BaseModel
from .common import BaseResponse
from typing import List, Optional

class RankingsPlayer(BaseModel):
    id: int
    rank: int
    player_name: str
    team: str
    total_fpts: float
    avg_fpts: float
    rank_change: int = 0

class RankingsResp(BaseResponse):
    data: List[RankingsPlayer]
