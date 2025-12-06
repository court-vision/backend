from pydantic import BaseModel
from .common import BaseResponse
from typing import List, Optional

class StandingsPlayer(BaseModel):
    rank: int
    player_name: str
    team: str
    total_fpts: float
    avg_fpts: float
    rank_change: int = 0  # Defaulting to 0 as per instruction to handle logic elsewhere

class StandingsResp(BaseResponse):
    data: List[StandingsPlayer]

