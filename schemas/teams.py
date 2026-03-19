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
    opponent_def_rating: Optional[float] = Field(None, description="Opponent's defensive rating (points allowed per 100 possessions)")


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


# ── NBA Team Stats ────────────────────────────────────────────────────────────

class NBATeamStatsData(BaseModel):
    """Season-to-date stats for an NBA team."""

    team: str
    team_name: str
    conference: str
    division: str
    as_of_date: str
    season: str
    gp: Optional[int] = None
    w: Optional[int] = None
    l: Optional[int] = None
    w_pct: Optional[float] = None
    pts: Optional[float] = None
    reb: Optional[float] = None
    ast: Optional[float] = None
    stl: Optional[float] = None
    blk: Optional[float] = None
    tov: Optional[float] = None
    fg_pct: Optional[float] = None
    fg3_pct: Optional[float] = None
    ft_pct: Optional[float] = None
    off_rating: Optional[float] = None
    def_rating: Optional[float] = None
    net_rating: Optional[float] = None
    pace: Optional[float] = None
    ts_pct: Optional[float] = None
    efg_pct: Optional[float] = None
    pie: Optional[float] = None


class NBATeamStatsResp(BaseResponse):
    """Response for GET /v1/teams/{abbrev}/stats."""

    data: Optional[NBATeamStatsData] = None


# ── NBA Team Roster ───────────────────────────────────────────────────────────

class NBATeamRosterPlayer(BaseModel):
    """Player on an NBA team roster with per-game averages."""

    player_id: int
    name: str
    position: Optional[str] = None
    gp: int
    pts: float
    reb: float
    ast: float
    stl: float
    blk: float
    tov: float
    fpts: float
    fg_pct: Optional[float] = None
    fg3_pct: Optional[float] = None
    ft_pct: Optional[float] = None
    injury_status: Optional[str] = None


class NBATeamRosterData(BaseModel):
    """Roster data for an NBA team."""

    team: str
    team_name: str
    players: list[NBATeamRosterPlayer]
    as_of_date: str


class NBATeamRosterResp(BaseResponse):
    """Response for GET /v1/teams/{abbrev}/roster."""

    data: Optional[NBATeamRosterData] = None


# ── NBA Team Live Game ────────────────────────────────────────────────────────

class TopPerformer(BaseModel):
    """Top performer in a game."""

    player_id: int
    name: str
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    min: int
    fgm: int
    fga: int
    fg3m: int


class InjuredPlayer(BaseModel):
    """Player with an injury status."""

    player_id: int
    name: str
    status: str
    injury_type: Optional[str] = None
    expected_return: Optional[str] = None


class NBATeamLiveGameData(BaseModel):
    """Current, most recent, or upcoming game for an NBA team."""

    game_id: Optional[str] = None
    game_date: str
    home_team: str
    away_team: str
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    status: str  # "scheduled" | "in_progress" | "final"
    period: Optional[int] = None
    game_clock: Optional[str] = None
    start_time_et: Optional[str] = None
    arena: Optional[str] = None
    home_periods: list[int] = Field(default_factory=list)
    away_periods: list[int] = Field(default_factory=list)
    home_top_performers: list[TopPerformer] = Field(default_factory=list)
    away_top_performers: list[TopPerformer] = Field(default_factory=list)
    injured_players: list[InjuredPlayer] = Field(default_factory=list)
    is_today: bool = False
    is_upcoming: bool = False


class NBATeamLiveGameResp(BaseResponse):
    """Response for GET /v1/teams/{abbrev}/live-game."""

    data: Optional[NBATeamLiveGameData] = None
