"""
Standard-league valuation: the pipeline-token route the data platform's
projections editor calls to rank a set of projections (`POST
/v1/internal/jobs/valuation/standard`, see `services.valuation.standard`), and
the one its daily_rankings pipeline calls for the board to store each morning
(`POST /v1/internal/jobs/rankings/daily`, see `services.valuation.daily`).
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseRequest, BaseResponse

# Far past any real pool (~520 players); a ceiling, not a target.
MAX_VALUATION_PLAYERS = 2000


class ValuationPlayer(BaseRequest):
    """One player's projection: a per-game line and the games it is expected over."""

    player_id: int = Field(description="NBA player id (nba.players.id)")
    line: dict[str, float] = Field(
        description=(
            "Per-game stats by canonical key (pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, "
            "ftm, fta, min). Unknown keys are ignored; percentages are derived from makes and attempts."
        ),
    )
    games: Optional[float] = Field(
        default=None, ge=0, le=82,
        description="Expected games this season. Omit when nothing projects them (valued at 65).",
    )
    team: Optional[str] = Field(
        default=None, max_length=8,
        description="Current NBA team tricode, for his schedule around the fantasy playoffs",
    )
    dd_rate: Optional[float] = Field(default=None, ge=0, le=1, description="Double-doubles per game")
    td_rate: Optional[float] = Field(default=None, ge=0, le=1, description="Triple-doubles per game")


class StandardValuationReq(BaseRequest):
    players: List[ValuationPlayer] = Field(
        min_length=1, max_length=MAX_VALUATION_PLAYERS,
        description=(
            "The whole pool to value together: a rank is a place among these players, and the "
            "category values are measured against the draftable cohort among them."
        ),
    )
    playoff_weight: Optional[float] = Field(
        default=None, ge=1.0, le=4.0,
        description="How many regular-season games one fantasy-playoff game counts as. Default 2.",
    )


class StandardRankResp(ApiModel):
    player_id: int
    points_rank: int = Field(description="Place among the players sent, standard points league")
    points_value: float = Field(description="Fantasy points per game under ESPN's default weights")
    points_season: float = Field(description="points_value x effective games: what points_rank is ordered by")
    category_rank: int = Field(description="Place among the players sent, standard 9-cat")
    category_value: float = Field(description="The 9-cat value index")
    category_score: float = Field(description="Summed per-category score the index is mapped from")
    games: float = Field(description="Effective games: expected games weighted by when his team plays them")


class StandardValuationData(ApiModel):
    league_size: int
    rounds: int
    playoff_weight: float
    playoff_weeks: List[int] = Field(
        description="The fantasy-playoff weeks weighted; empty when the season calendar is unavailable"
    )
    players: List[StandardRankResp] = Field(description="One entry per player sent, in the order sent")


class StandardValuationResp(BaseResponse):
    data: Optional[StandardValuationData] = None


class DailyBoardPlace(ApiModel):
    rank: int = Field(description="Place on this board, 1 is best")
    value: float = Field(description="The per-game number the board shows: fantasy points, or the 9-cat index")
    score: float = Field(
        description="What the rank is ordered by: fantasy points over the games left, or the summed category score"
    )
    games: float = Field(description="The games he is expected to play from the board's day on")


class DailyRankResp(ApiModel):
    player_id: int = Field(description="NBA player id (nba.players.id)")
    name: str
    team: Optional[str] = Field(description="Current NBA team tricode")
    line_source: str = Field(
        description="Where his line came from: live (the morning's live projection), projection, season or baseline"
    )
    season_gp: Optional[int] = Field(description="Games played this season; null before opening night")
    injury_status: Optional[str] = Field(description="Today's flag (OUT, DAY_TO_DAY, ...); null when he plays")
    points: DailyBoardPlace = Field(description="Standard points league, ESPN's default weights")
    categories: DailyBoardPlace = Field(description="Standard nine-category league")


class DailyRankingsData(ApiModel):
    season: str
    as_of: date = Field(description="The fantasy day the boards are for: games from this day on are counted")
    in_season: bool = Field(description="False before opening night and after the regular season")
    model: str = Field(
        description=(
            "What built the boards; stored with every row. three-part-1: the morning's live "
            "projection. season-blend-1: the preseason projection blended with this season's "
            "games, on a morning with no fresh live projection. projection-1: before opening night"
        ),
    )
    live_as_of: Optional[date] = Field(
        default=None, description="The morning of the live projection the boards read; null without one"
    )
    season_as_of: Optional[date] = Field(description="The newest box score the lines read; null out of season")
    projections_as_of: Optional[date]
    projection_source: Optional[str]
    prior_games: int = Field(description="The projection's weight against this season, in games")
    league_size: int
    rounds: int
    playoff_weight: float
    players: List[DailyRankResp] = Field(description="Every valued player, in points-board order")


class DailyRankingsResp(BaseResponse):
    data: Optional[DailyRankingsData] = None
