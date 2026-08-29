from datetime import date
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field

from .common import BaseResponse, CategoryDefResp


def _coerce_int(value):
    """Query values arrive as strings; Literal[7, 14, 30] only matches ints."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# The rolling window every rankings endpoint accepts. Shared so the public and
# league routes cannot drift on which windows exist.
RollingWindow = Annotated[Literal[7, 14, 30], BeforeValidator(_coerce_int)]


class RankingsPlayer(BaseModel):
    id: int
    rank: int
    player_name: str
    team: str
    total_fpts: float
    avg_fpts: float
    rank_change: int = Field(
        default=0,
        description=(
            "Change in league-wide rank versus 7 days ago; positive is an improvement. "
            "0 for rolling-window and category rankings, which do not track movement, "
            "and for everyone during the first week of a season."
        ),
    )

    # Populated for format=categories only (None for points rankings).
    gp: Optional[int] = None
    categories: Optional[dict[str, Optional[float]]] = Field(
        default=None,
        description="Per-game value per category; rates are 0-1 fractions (None when the player has no attempts)",
    )
    category_z: Optional[dict[str, float]] = Field(
        default=None,
        description="Signed z-score per category over the ranked pool (positive is always good; TOV is inverted)",
    )
    score: Optional[float] = Field(default=None, description="Sum of category z-scores; the ranking key")


class RankingsScoring(BaseModel):
    """What the ranking was scored by.

    `points` rankings are not universal: the stored `fpts` columns every points
    ranking sorts on are one hardcoded formula (one ESPN league's settings). This
    block says so, and says whose settings were used when a league supplied them.
    """

    basis: Literal["default_points", "league_points", "categories"] = Field(
        description=(
            "default_points: the platform's default points formula. "
            "league_points: the league's own point weights. "
            "categories: summed per-category z-scores (see `categories` on the meta)."
        ),
    )
    point_weights: Optional[dict[str, float]] = Field(
        default=None, description="Per-stat point values used; null for category rankings",
    )
    league_id: Optional[int] = None
    league_name: Optional[str] = None
    settings_synced: Optional[bool] = Field(
        default=None,
        description=(
            "Whether the league's settings were successfully read from the provider. "
            "False means the defaults below were substituted. Null when no league is involved."
        ),
    )
    unsupported: list[str] = Field(
        default=[],
        description="League scoring keys that could not be honored (e.g. dd/td, which need per-game detail)",
    )


class RankingsMeta(BaseModel):
    format: str                                 # points | categories
    window: Optional[int] = None                # 7 | 14 | 30, None for full season
    as_of: Optional[date] = None                # date the underlying snapshot runs through
    categories: list[CategoryDefResp] = []      # empty for points rankings
    pool_size: int                              # players ranked (after any min_games filter)
    min_games: Optional[int] = None             # games-played floor applied (categories only)
    season: Optional[str] = None                # season the data belongs to, e.g. "2026-27"
    season_day: Optional[int] = None            # 1-based day of the regular season the data runs through
    max_gp: Optional[int] = None                # most games any ranked player has played
    scoring: Optional[RankingsScoring] = None   # what the ranking was scored by


class RankingsResp(BaseResponse):
    data: List[RankingsPlayer]
    meta: Optional[RankingsMeta] = None
