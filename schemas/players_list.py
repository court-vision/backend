"""
Schemas for player list API responses.
"""

from typing import Optional
from pydantic import BaseModel, Field
from schemas.common import BaseResponse


class PlayerListItem(BaseModel):
    """Individual player in the players list."""

    id: int = Field(..., description="NBA player ID")
    espn_id: Optional[int] = Field(None, description="ESPN player ID")
    name: str = Field(..., description="Player name")
    team: Optional[str] = Field(None, description="Team abbreviation")
    position: Optional[str] = Field(None, description="Player position")
    games_played: int = Field(..., description="Games played this season")
    avg_fpts: float = Field(..., description="Average fantasy points per game")
    rank: Optional[int] = Field(None, description="Current fantasy ranking")


class PlayersListData(BaseModel):
    """Response data for player list."""

    players: list[PlayerListItem] = Field(default_factory=list, description="List of players")
    total: int = Field(..., description="Total number of players matching filters")
    limit: int = Field(..., description="Number of results returned")
    offset: int = Field(..., description="Offset from start")


class PlayersListResp(BaseResponse):
    """Response for GET /v1/players."""

    data: Optional[PlayersListData] = None
