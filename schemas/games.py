"""
Schemas for game-related API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class GameInfo(BaseModel):
    """Individual game information."""

    game_id: Optional[str] = Field(None, description="NBA game ID")
    game_date: str = Field(..., description="Date of the game (YYYY-MM-DD)")
    home_team: str = Field(..., description="Home team abbreviation")
    away_team: str = Field(..., description="Away team abbreviation")
    home_score: Optional[int] = Field(None, description="Home team score (null if not started)")
    away_score: Optional[int] = Field(None, description="Away team score (null if not started)")
    status: str = Field(..., description="Game status: scheduled, in_progress, or final")
    arena: Optional[str] = Field(None, description="Arena name")
    period: Optional[int] = Field(None, description="Current period (null if not started)")
    game_clock: Optional[str] = Field(None, description="Remaining time in period (ISO 8601 duration, null if not live)")


class GamesOnDateData(BaseModel):
    """Response data for games on a specific date."""

    date: str = Field(..., description="Date queried (YYYY-MM-DD)")
    games: list[GameInfo] = Field(default_factory=list, description="List of games on this date")
    count: int = Field(..., description="Number of games on this date")


class GamesOnDateResp(BaseResponse):
    """Response for GET /v1/games/{date}."""

    data: Optional[GamesOnDateData] = None
