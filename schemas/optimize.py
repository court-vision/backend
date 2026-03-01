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
    """Response for POST /v1/analytics/generate-lineup."""

    data: Optional[OptimizeData] = None


class GenerateLineupRequest(BaseModel):
    """Request body for lineup generation from a connected team."""

    team_id: int = Field(..., description="ID of the user's stored team (from manage-teams)")
    week: int = Field(..., ge=1, le=26, description="Fantasy week to optimize for")
    streaming_slots: int = Field(default=2, ge=0, le=10, description="Number of streaming add/drop moves to consider")
    use_recent_stats: bool = Field(default=False, description="Use decay-weighted recent stats instead of season averages")
