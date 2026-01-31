"""
Schemas for team-related API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class ScheduleGame(BaseModel):
    """Individual game in a team's schedule."""

    date: str = Field(..., description="Game date (YYYY-MM-DD)")
    opponent: str = Field(..., description="Opponent team abbreviation")
    home: bool = Field(..., description="Whether this is a home game")
    back_to_back: bool = Field(..., description="Whether this game is a back-to-back")
    status: str = Field(..., description="Game status: scheduled, in_progress, or final")
    team_score: Optional[int] = Field(None, description="Team's score (if game completed)")
    opponent_score: Optional[int] = Field(None, description="Opponent's score (if game completed)")


class TeamScheduleData(BaseModel):
    """Response data for a team's schedule."""

    team: str = Field(..., description="Team abbreviation")
    team_name: str = Field(..., description="Full team name")
    schedule: list[ScheduleGame] = Field(default_factory=list, description="List of games")
    remaining_games: int = Field(..., description="Number of remaining scheduled games")
    total_games: int = Field(..., description="Total games in response")


class TeamScheduleResp(BaseResponse):
    """Response for GET /v1/teams/{abbrev}/schedule."""

    data: Optional[TeamScheduleData] = None
