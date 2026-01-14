from pydantic import BaseModel, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, LeagueInfo


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
    current_score: float                   # Points scored so far this matchup period
    projected_score: float                 # Projected final score for matchup period
    roster: list[MatchupPlayerResp]


class MatchupData(BaseModel):
    """Complete matchup data structure"""
    matchup_period: int                    # Week/matchup period number
    matchup_period_start: str              # ISO date string
    matchup_period_end: str                # ISO date string
    your_team: MatchupTeamResp
    opponent_team: MatchupTeamResp
    projected_winner: str                  # Team name of projected winner
    projected_margin: float                # Projected point differential


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
