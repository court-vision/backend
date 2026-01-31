"""
Schemas for lineup optimization API.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class PlayerInput(BaseModel):
    """Player data for optimization input."""

    id: int = Field(..., description="ESPN player ID")
    name: str = Field(..., description="Player name")
    team: str = Field(..., description="Team abbreviation")
    position: str = Field(..., description="Player position (PG, SG, SF, PF, C)")
    avg_fpts: float = Field(..., description="Average fantasy points")
    injury_status: Optional[str] = Field(None, description="Injury status if any")


class OptimizeRequest(BaseModel):
    """Request body for lineup optimization."""

    roster: list[PlayerInput] = Field(
        ..., description="Current roster players", min_length=1
    )
    free_agents: list[PlayerInput] = Field(
        default=[], description="Available free agents to consider"
    )
    week: int = Field(..., ge=1, le=26, description="Fantasy week number")
    threshold: float = Field(
        default=30.0, ge=0, le=100, description="Minimum fpts threshold for streaming"
    )


class RecommendedMove(BaseModel):
    """A recommended roster move."""

    action: str = Field(..., description="Action type: 'add', 'drop', 'stream'")
    player_add: Optional[PlayerInput] = Field(None, description="Player to add")
    player_drop: Optional[PlayerInput] = Field(None, description="Player to drop")
    reason: str = Field(..., description="Explanation for the move")
    projected_gain: float = Field(..., description="Projected fpts gain from move")


class OptimizedDay(BaseModel):
    """Optimized lineup for a single day."""

    date: str = Field(..., description="Date (YYYY-MM-DD)")
    active_players: list[str] = Field(..., description="Player names in active slots")
    bench_players: list[str] = Field(..., description="Player names on bench")
    projected_fpts: float = Field(..., description="Projected fantasy points")


class OptimizeData(BaseModel):
    """Response data for lineup optimization."""

    week: int = Field(..., description="Fantasy week")
    projected_total_fpts: float = Field(..., description="Total projected fpts for week")
    daily_lineups: list[OptimizedDay] = Field(
        default=[], description="Optimized lineup for each day"
    )
    recommended_moves: list[RecommendedMove] = Field(
        default=[], description="Recommended roster moves"
    )
    optimization_notes: list[str] = Field(
        default=[], description="Additional notes about optimization"
    )


class OptimizeResp(BaseResponse):
    """Response for POST /v1/analytics/optimize."""

    data: Optional[OptimizeData] = None
