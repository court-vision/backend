from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field

from .common import BaseResponse, CategoryDefResp


class RankingsPlayer(BaseModel):
    id: int
    rank: int
    player_name: str
    team: str
    total_fpts: float
    avg_fpts: float
    rank_change: int = 0

    # Populated for format=categories only (None for points rankings).
    gp: Optional[int] = None
    categories: Optional[dict[str, Optional[float]]] = Field(
        default=None,
        description="Per-game value per category; rates are 0-1 fractions (None when the player has no attempts)",
    )
    category_z: Optional[dict[str, float]] = Field(
        default=None,
        description="Signed z-score per category over the ranked pool (positive is always good; TOV is inverted)",
    )
    score: Optional[float] = Field(default=None, description="Sum of category z-scores; the ranking key")


class RankingsMeta(BaseModel):
    format: str                                 # points | categories
    window: Optional[int] = None                # 7 | 14 | 30, None for full season
    as_of: Optional[date] = None                # date the underlying snapshot runs through
    categories: list[CategoryDefResp] = []      # empty for points rankings
    pool_size: int                              # players ranked (after any min_games filter)
    min_games: Optional[int] = None             # games-played floor applied (categories only)


class RankingsResp(BaseResponse):
    data: List[RankingsPlayer]
    meta: Optional[RankingsMeta] = None
