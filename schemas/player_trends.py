"""
Schemas for player trend API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class TrendPeriod(BaseModel):
    """Stats for a trend period."""

    avg_fpts: float = Field(..., description="Average fantasy points per game")
    games: int = Field(..., description="Number of games in period")


class OwnershipTrend(BaseModel):
    """Ownership trend data."""

    current: float = Field(..., description="Current ownership percentage")
    change_7d: float = Field(..., description="Change over last 7 days")


class PlayerTrendsData(BaseModel):
    """Response data for player trends."""

    player_id: int = Field(..., description="NBA player ID")
    player_name: str = Field(..., description="Player name")
    team: Optional[str] = Field(None, description="Current team abbreviation")
    current_rank: Optional[int] = Field(None, description="Current fantasy ranking")
    trends: dict[str, TrendPeriod] = Field(
        default_factory=dict,
        description="Trend data for different periods (last_7_days, last_14_days, last_30_days)",
    )
    ownership: Optional[OwnershipTrend] = Field(None, description="Ownership trend data")


class PlayerTrendsResp(BaseResponse):
    """Response for GET /v1/players/{id}/trends."""

    data: Optional[PlayerTrendsData] = None
