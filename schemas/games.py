"""
Schemas for game-related API responses.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field
from schemas.common import ApiModel, BaseResponse
from schemas.teams import GameScoreSnapshot


class GameInfo(ApiModel):
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
    start_time_et: Optional[str] = Field(None, description="Scheduled tip-off time in ET (HH:MM format)")


class GamesOnDateData(ApiModel):
    """Response data for games on a specific date."""

    date: str = Field(..., description="Date queried (YYYY-MM-DD)")
    games: list[GameInfo] = Field(default_factory=list, description="List of games on this date")
    count: int = Field(..., description="Number of games on this date")


class GamesOnDateResp(BaseResponse):
    """Response for GET /v1/games/{date}."""

    data: Optional[GamesOnDateData] = None


class GameLine(ApiModel):
    """One player's line in one game."""

    player_id: int = Field(..., description="NBA player ID")
    espn_id: Optional[int] = Field(None, description="ESPN player ID, when mapped")
    player_name: str
    team: Optional[str] = Field(None, description="Abbreviation of the team the player played for; null when it cannot be told")
    min: int = Field(..., description="Whole minutes played (a cameo under a minute is 0)")
    fpts: int
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    plus_minus: Optional[int] = Field(None, description="Plus-minus; null on live lines and older settled rows")


class GameDetailData(ApiModel):
    """One game as it stood at its last read: the final buzzer, for a game that is over."""

    game: GameInfo
    home_periods: list[int] = Field(default_factory=list, description="Home points per period (Q1-Q4, then each OT); empty when the league feed could not be read")
    away_periods: list[int] = Field(default_factory=list, description="Away points per period (Q1-Q4, then each OT)")
    score_history: list[GameScoreSnapshot] = Field(default_factory=list, description="The score through the game, oldest first, from tip to final; empty when no snapshots were kept")
    lines: list[GameLine] = Field(default_factory=list, description="Every player who took the floor, by fantasy points")
    lines_source: Optional[Literal["settled", "live"]] = Field(
        None,
        description="settled: the game log the post-game run wrote; live: the live box score's last read, until the game log lands; null: no lines on record",
    )


class GameDetailResp(BaseResponse):
    """Response for GET /v1/games/{date}/{game_id}."""

    data: Optional[GameDetailData] = None
