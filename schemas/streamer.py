from pydantic import BaseModel, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, LeagueInfo


class StreamerPlayerResp(BaseModel):
    """A streaming candidate player."""
    player_id: int
    name: str
    team: str
    valid_positions: list[str]

    # Performance metrics
    avg_points_last_7: Optional[float] = None
    avg_points_season: float

    # Schedule metrics
    games_remaining: int
    has_b2b: bool
    b2b_game_count: int
    game_days: list[int]

    # Ranking
    streamer_score: float

    # Status
    injured: bool
    injury_status: Optional[str] = None


class StreamerData(BaseModel):
    """Complete streamer search results."""
    matchup_number: int
    current_day_index: int
    teams_with_b2b: list[str]
    streamers: list[StreamerPlayerResp]


class StreamerReq(BaseRequest):
    """Request for finding streamers."""
    league_info: LeagueInfo
    fa_count: int = Field(default=50, ge=10, le=200)
    exclude_injured: bool = Field(default=True)
    b2b_only: bool = Field(
        default=False,
        description="Only show players on teams with remaining B2Bs"
    )
    day: Optional[int] = Field(
        default=None,
        ge=0,
        description="Day index within the matchup (0-indexed). If None, uses current day."
    )


class StreamerResp(BaseResponse):
    """Response containing streamer candidates."""
    data: Optional[StreamerData] = None
