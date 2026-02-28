from datetime import date
from typing import Optional

from pydantic import BaseModel

from schemas.common import BaseResponse


class BreakoutBeneficiary(BaseModel):
    """The player expected to benefit from the minutes vacuum."""
    player_id: int
    name: str
    team: str
    position: str
    depth_rank: int          # Position-group depth chart rank (2 = first backup, etc.)
    avg_min: float
    avg_fpts: float
    games_remaining: int
    has_b2b: bool


class BreakoutInjuredPlayer(BaseModel):
    """The prominent injured player creating the opportunity."""
    player_id: int
    name: str
    avg_min: float
    status: str  # "Out" or "Doubtful"
    expected_return: Optional[date] = None


class BreakoutSignals(BaseModel):
    """Evidence supporting this breakout recommendation."""
    depth_rank: int
    projected_min_boost: float
    # Position-validated opportunity game stats (null when insufficient data)
    opp_min_avg: Optional[float] = None
    opp_fpts_avg: Optional[float] = None
    opp_game_count: int = 0
    breakout_score: float


class BreakoutCandidateResp(BaseModel):
    """A single breakout streamer recommendation."""
    beneficiary: BreakoutBeneficiary
    injured_player: BreakoutInjuredPlayer
    signals: BreakoutSignals


class BreakoutData(BaseModel):
    as_of_date: date
    candidates: list[BreakoutCandidateResp]


class BreakoutResp(BaseResponse):
    data: Optional[BreakoutData] = None
