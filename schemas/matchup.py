from pydantic import BaseModel, Field
from typing import Literal, Optional
from .common import BaseRequest, BaseResponse, LeagueInfo


# ------------------------------- Category Scoring Models ------------------------------- #

ScoringFormat = Literal["points", "categories"]


class CategoryScoreItem(BaseModel):
    """One category in a head-to-head comparison."""
    key: str
    label: str
    you: float
    opp: float
    winner: Literal["you", "opp", "tie"]
    higher_is_better: bool
    is_rate: bool


class CategoryComparison(BaseModel):
    items: list[CategoryScoreItem]
    wins: int
    losses: int
    ties: int


class CategoryTeamScore(BaseModel):
    """A team's per-category totals (rates as 0-1 fractions) and record."""
    totals: dict[str, float]
    raw: Optional[dict[str, float]] = None    # fgm/fga/ftm/fta/fg3m/fg3a when known
    wins: int = 0
    losses: int = 0
    ties: int = 0
    live_adjusted: bool = False


# ------------------------------- Matchup Data Models ------------------------------- #

class MatchupPlayerResp(BaseModel):
    """Player data within a matchup context"""
    player_id: int
    name: str
    team: str                              # NBA team abbreviation
    position: str                          # Primary position (PG, SG, etc.)
    lineup_slot: str                       # Current lineup slot (PG, SG, BE, IR, etc.)
    avg_points: float                      # Average points based on selected window
    projected_points: float                # ESPN's projected points
    games_remaining: int                   # Games left in matchup period
    injured: bool
    injury_status: Optional[str] = None


class MatchupTeamResp(BaseModel):
    """Team data within a matchup"""
    team_name: str
    team_id: int                           # ESPN fantasy team ID
    current_score: float                   # Points so far (category leagues: categories won)
    projected_score: float                 # Projected final score (category leagues: projected categories won)
    roster: list[MatchupPlayerResp]
    categories: Optional[CategoryTeamScore] = None   # category leagues only


class MatchupData(BaseModel):
    """Complete matchup data structure"""
    matchup_period: int                    # Week/matchup period number
    matchup_period_start: str              # ISO date string
    matchup_period_end: str                # ISO date string
    your_team: MatchupTeamResp
    opponent_team: MatchupTeamResp
    projected_winner: str                  # Team name of projected winner
    projected_margin: float                # Projected point differential (category leagues: won - lost)
    # Day watermark (1 = opening night): the first season day NOT yet included
    # in the team scores above. ESPN reports it as status.latestScoringPeriod;
    # Yahoo has none, so it is derived from our calendar -- hence the source.
    scoring_period_id: int | None = None
    scoring_period_source: Literal["provider", "calendar", "unknown"] = "unknown"
    schedule_week: int | None = None       # Our calendar week containing matchup_period_start (optimizer input)
    scoring_format: ScoringFormat = "points"
    settings_synced: bool = False
    category_comparison: Optional[CategoryComparison] = None
    projected_category_comparison: Optional[CategoryComparison] = None


# ------------------------------- Request/Response Models ------------------------------- #

class MatchupReq(BaseRequest):
    """Request for getting matchup data"""
    league_info: LeagueInfo
    avg_window: str = Field(
        default="season",
        pattern="^(season|last_7|last_14|last_30)$",
        description="Averaging window for projections: season, last_7, last_14, or last_30"
    )


class MatchupResp(BaseResponse):
    """Response containing matchup data"""
    data: Optional[MatchupData] = None


# ------------------------------- Daily Score History Models ------------------------------- #

class DailyScorePoint(BaseModel):
    """Single day's score snapshot for chart visualization"""
    date: str                              # ISO date string
    day_of_matchup: int                    # 0-indexed day within matchup
    your_score: float
    opponent_score: float
    your_categories: Optional[dict[str, float]] = None
    opponent_categories: Optional[dict[str, float]] = None


class MatchupScoreHistory(BaseModel):
    """Historical score data for a matchup period"""
    team_id: int
    team_name: str
    opponent_team_name: str
    matchup_period: int
    history: list[DailyScorePoint]
    scoring_format: ScoringFormat = "points"


class MatchupScoreHistoryResp(BaseResponse):
    """Response containing matchup score history"""
    data: Optional[MatchupScoreHistory] = None


# ------------------------------- Live Matchup Models ------------------------------- #

class PlayerLiveStats(BaseModel):
    """Live in-game stat overlay for a player (from live_player_stats table)."""
    nba_player_id: int
    live_fpts: float                  # scored with the league's point weights
    live_pts: int
    live_reb: int
    live_ast: int
    live_stl: int
    live_blk: int
    live_tov: int
    live_min: int
    live_fgm: int = 0
    live_fga: int = 0
    live_fg3m: int = 0
    live_fg3a: int = 0
    live_ftm: int = 0
    live_fta: int = 0
    game_status: int               # 1=scheduled, 2=in_progress, 3=final
    period: Optional[int] = None
    game_clock: Optional[str] = None
    last_updated: Optional[str] = None


class LiveMatchupPlayer(MatchupPlayerResp):
    """MatchupPlayerResp extended with live game stats (None if no game today)."""
    live: Optional[PlayerLiveStats] = None


class LiveMatchupTeam(BaseModel):
    team_name: str
    team_id: int
    current_score: float           # From ESPN/Yahoo (live, correct custom scoring)
    projected_score: float
    roster: list[LiveMatchupPlayer]
    categories: Optional[CategoryTeamScore] = None


class LiveMatchupData(BaseModel):
    matchup_period: int
    matchup_period_start: str
    matchup_period_end: str
    your_team: LiveMatchupTeam
    opponent_team: LiveMatchupTeam
    projected_winner: str
    projected_margin: float
    game_date: str                 # the single day of live stats overlaid on the baseline
    # Days the stored baseline is missing (the live rows for them have already
    # been deleted, so the score is knowably short). 0 when fresh.
    baseline_stale_days: int = 0
    scoring_format: ScoringFormat = "points"
    settings_synced: bool = False
    category_comparison: Optional[CategoryComparison] = None


class LiveMatchupResp(BaseResponse):
    data: Optional[LiveMatchupData] = None


# ------------------------------- Daily Matchup Models ------------------------------- #

class DailyMatchupPlayerStats(BaseModel):
    """Player stats for a single past day. No lineup_slot since we don't snapshot rosters."""
    player_id: int                             # ESPN player ID (from roster)
    name: str
    team: str                                  # NBA team abbreviation
    position: str
    nba_player_id: Optional[int] = None        # Resolved NBA player ID
    had_game: bool                             # Whether their team had a game that day
    fpts: Optional[float] = None               # scored with the league's point weights
    pts: Optional[int] = None
    reb: Optional[int] = None
    ast: Optional[int] = None
    stl: Optional[int] = None
    blk: Optional[int] = None
    tov: Optional[int] = None
    min: Optional[int] = None
    fgm: Optional[int] = None
    fga: Optional[int] = None
    fg3m: Optional[int] = None
    fg3a: Optional[int] = None
    ftm: Optional[int] = None
    fta: Optional[int] = None


class DailyMatchupFuturePlayer(BaseModel):
    """Player info for a future day. Shows whether they have a game."""
    player_id: int
    name: str
    team: str
    position: str
    has_game: bool
    opponent: Optional[str] = None             # e.g., "vs LAL" or "@ BOS"
    game_time_et: Optional[str] = None         # e.g., "19:30"
    injured: bool
    injury_status: Optional[str] = None


class DailyMatchupTeam(BaseModel):
    """Team data for a daily matchup view."""
    team_name: str
    team_id: int
    total_fpts: Optional[float] = None         # Sum of roster fpts (past days only)
    roster: list[DailyMatchupPlayerStats] | list[DailyMatchupFuturePlayer]
    categories: Optional[dict[str, float]] = None   # category leagues: day totals per category


class DailyMatchupData(BaseModel):
    """Response data for daily matchup drill-down."""
    date: str                                  # ISO date string (YYYY-MM-DD)
    day_type: str                              # "past", "today", "future"
    day_of_week: str                           # "Mon", "Tue", etc.
    day_index: int                             # 0-indexed from matchup start
    matchup_period: int
    matchup_period_start: str
    matchup_period_end: str
    your_team: DailyMatchupTeam
    opponent_team: DailyMatchupTeam
    scoring_format: ScoringFormat = "points"
    category_comparison: Optional[CategoryComparison] = None


class DailyMatchupResp(BaseResponse):
    """Response containing daily matchup drill-down data."""
    data: Optional[DailyMatchupData] = None


# ------------------------------- Weekly Matchup Models ------------------------------- #

class WeeklyMatchupData(BaseModel):
    """All days in the current matchup period, computed in a single ESPN call."""
    matchup_period: int
    days: list[DailyMatchupData]


class WeeklyMatchupResp(BaseResponse):
    """Response containing all days for the current matchup period."""
    data: Optional[WeeklyMatchupData] = None


# ------------------------------- Season Summary Models ------------------------------- #

class WeekResult(BaseModel):
    matchup_period: int
    opponent_team_name: str
    points_for: float
    points_against: float
    won: bool
    categories_won: Optional[int] = None
    categories_lost: Optional[int] = None
    categories_tied: Optional[int] = None


class SeasonSummaryData(BaseModel):
    team_id: int
    team_name: str
    wins: int
    losses: int
    total_points_for: float
    total_points_against: float
    best_week: Optional[WeekResult] = None
    worst_week: Optional[WeekResult] = None
    weeks: list[WeekResult]
    scoring_format: ScoringFormat = "points"


class SeasonSummaryResp(BaseResponse):
    data: Optional[SeasonSummaryData] = None
