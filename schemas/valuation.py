"""
Standard-league valuation: the pipeline-token route the data platform's
projections editor calls to rank a set of projections (`POST
/v1/internal/jobs/valuation/standard`). See `services.valuation.standard`.
"""

from __future__ import annotations

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
