"""
Schemas for player game log API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class GameLog(BaseModel):
    """Individual game in a player's game log."""

    date: str = Field(..., description="Game date (YYYY-MM-DD)")
    opponent: Optional[str] = Field(None, description="Opponent team abbreviation")
    home: Optional[bool] = Field(None, description="Whether this was a home game")
    fpts: int = Field(..., description="Fantasy points scored")
    pts: int = Field(..., description="Points")
    reb: int = Field(..., description="Rebounds")
    ast: int = Field(..., description="Assists")
    stl: int = Field(..., description="Steals")
    blk: int = Field(..., description="Blocks")
    tov: int = Field(..., description="Turnovers")
    min: int = Field(..., description="Minutes played")
    fgm: int = Field(..., description="Field goals made")
    fga: int = Field(..., description="Field goals attempted")
    fg3m: int = Field(..., description="Three-pointers made")
    fg3a: int = Field(..., description="Three-pointers attempted")
    ftm: int = Field(..., description="Free throws made")
    fta: int = Field(..., description="Free throws attempted")


class PlayerGamesData(BaseModel):
    """Response data for a player's game log."""

    player_id: int = Field(..., description="NBA player ID")
    player_name: str = Field(..., description="Player name")
    team: Optional[str] = Field(None, description="Current team abbreviation")
    games: list[GameLog] = Field(default_factory=list, description="List of games")
    total_games: int = Field(..., description="Number of games in response")


class PlayerGamesResp(BaseResponse):
    """Response for GET /v1/players/{id}/games."""

    data: Optional[PlayerGamesData] = None
