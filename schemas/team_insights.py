from pydantic import BaseModel
from typing import Optional
from .common import BaseResponse


class PlayerScheduleInfo(BaseModel):
    """Schedule info for a player's NBA team in the current matchup."""
    game_days: list[int]       # Day indices within matchup (0-indexed)
    games_remaining: int
    has_b2b: bool


class EnrichedRosterPlayer(BaseModel):
    """Roster player with schedule and multi-window stat data."""
    # Base fields (from PlayerResp)
    player_id: int
    name: str
    avg_points: float          # Season avg FPTS
    team: str                  # NBA team abbreviation
    valid_positions: list[str]
    injured: bool
    injury_status: Optional[str] = None

    # Enriched fields
    schedule: Optional[PlayerScheduleInfo] = None
    avg_fpts_l7: Optional[float] = None
    avg_fpts_l14: Optional[float] = None
    avg_fpts_l30: Optional[float] = None


class CategoryStrengths(BaseModel):
    """Team aggregate averages per stat category (L14 window)."""
    avg_points: float
    avg_rebounds: float
    avg_assists: float
    avg_steals: float
    avg_blocks: float
    avg_turnovers: float
    avg_fg_pct: float
    avg_ft_pct: float


class ScheduleOverview(BaseModel):
    """Current matchup week schedule context."""
    matchup_number: int
    matchup_start: str         # ISO date
    matchup_end: str           # ISO date
    current_day_index: int
    game_span: int
    total_team_games: int      # Sum of remaining games across roster
    teams_with_b2b: list[str]  # NBA teams on roster with B2Bs remaining
    day_game_counts: list[int] # Per-day count of roster players with games


class RosterHealthSummary(BaseModel):
    """Counts of players by health status."""
    total_players: int
    healthy: int
    out: int                   # OUT, IL, IL+, O
    day_to_day: int            # DTD, DAY_TO_DAY
    game_time_decision: int    # GTD


class TeamInsightsData(BaseModel):
    """Complete team insights response."""
    roster: list[EnrichedRosterPlayer]
    category_strengths: Optional[CategoryStrengths] = None
    schedule_overview: Optional[ScheduleOverview] = None
    roster_health: RosterHealthSummary
    projected_week_fpts: Optional[float] = None


class TeamInsightsResp(BaseResponse):
    data: Optional[TeamInsightsData] = None
