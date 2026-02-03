"""
Schemas for ownership trend API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class TrendingPlayer(BaseModel):
    """Player in trending list."""

    player_id: int = Field(..., description="NBA player ID")
    player_name: str = Field(..., description="Player name")
    team: Optional[str] = Field(None, description="Team abbreviation")
    current_ownership: float = Field(..., description="Current ownership percentage")
    previous_ownership: float = Field(..., description="Ownership at start of period")
    change: float = Field(..., description="Change in ownership percentage points")
    velocity: float = Field(
        ..., description="Relative change as percentage (change / previous * 100)"
    )


class OwnershipTrendingData(BaseModel):
    """Response data for ownership trending."""

    days: int = Field(..., description="Lookback period in days")
    min_ownership: float = Field(..., description="Minimum ownership filter applied")
    sort_by: str = Field(..., description="Sort method used (velocity or change)")
    trending_up: list[TrendingPlayer] = Field(
        default_factory=list, description="Players with rising ownership"
    )
    trending_down: list[TrendingPlayer] = Field(
        default_factory=list, description="Players with falling ownership"
    )


class OwnershipTrendingResp(BaseResponse):
    """Response for GET /v1/ownership/trending."""

    data: Optional[OwnershipTrendingData] = None
