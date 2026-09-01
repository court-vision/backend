"""Draft Lab board schemas (v0): ranked, cap-aware board rows for one league."""

from datetime import date
from typing import List, Literal, Optional

from pydantic import Field

from .common import ApiModel, BaseResponse, CategoryDefResp


class DraftBoardRow(ApiModel):
    player_id: int = Field(description="NBA player id (nba.players.id) — the id terminal panels expect")
    espn_id: Optional[int] = None
    name: str
    team: Optional[str] = Field(
        default=None,
        description="NBA team abbreviation from last season's stats; None for rookies (and anyone without a baseline row)",
    )
    position: Optional[str] = Field(default=None, description="NBA-style position (G, F, C, F-C, ...)")
    cv_rank: int = Field(
        description=(
            "Rank by CV value over the full pool, picked players included — a pre-draft big "
            "board rank that stays stable (and comparable to market_rank) as picks remove rows"
        ),
    )
    value: float = Field(
        description="League-scored per-game value: fantasy points under the league's weights, or the fpts-scale category value",
    )
    value_source: Literal["projection", "baseline"] = Field(
        description="projection: ESPN's published per-game projection. baseline: last season's per-game averages.",
    )
    last_season_gp: Optional[int] = Field(default=None, description="Games played last season; None for rookies")
    projected_gp: Optional[int] = Field(default=None, description="Projected games this season, when a projection exists")
    fpts_avg: float = Field(description="Per-game fantasy points under the platform default formula (familiar scale, tiebreak)")
    market_rank: Optional[int] = Field(default=None, description="ESPN editorial overall draft rank (latest snapshot)")
    adp: Optional[float] = Field(default=None, description="Average draft position across real ESPN drafts")
    auction_value: Optional[float] = Field(default=None, description="ESPN editorial auction value")
    market_delta: Optional[int] = Field(
        default=None,
        description="market_rank − cv_rank; positive means the market ranks the player worse than CV does (a bargain)",
    )
    cap_blocked: bool = Field(
        default=False,
        description=(
            "Drafting this player would exceed a hard per-position roster cap "
            "(league position_limits vs the caller's current roster). Shown greyed with a CAP badge, never hidden."
        ),
    )

    # Populated for category leagues only (None for points leagues).
    categories: Optional[dict[str, Optional[float]]] = Field(
        default=None,
        description="Per-game value per category; rates are 0-1 fractions (None when the player has no attempts)",
    )
    category_z: Optional[dict[str, float]] = Field(
        default=None,
        description="Signed z-score per category over the full pool (positive is always good; TOV is inverted)",
    )
    score: Optional[float] = Field(default=None, description="Sum of category z-scores; what `value` is mapped from")


class DraftBoardMeta(ApiModel):
    season: str                                 # season the board is for, e.g. "2026-27"
    format: str                                 # points | categories
    value_kind: Literal["fpts", "cat_value"]    # what the `value` column measures
    pool_size: int                              # full pool (picked included) — the cv_rank denominator
    available: int                              # rows returned (pool minus picked)
    projection_count: int                       # pool rows valued from a projection
    baseline_count: int                         # pool rows valued from last season's baseline
    projections_as_of: Optional[date] = None    # snapshot date of the projections used (None before ESPN publishes)
    market_as_of: Optional[date] = None         # snapshot date of the market ranks used
    position_limits: dict[str, int] = {}        # the league's hard per-position caps, as stored ({"C": 4})
    categories: list[CategoryDefResp] = []      # empty for points leagues
    settings_synced: Optional[bool] = None      # whether the league's settings were read from the provider
    unsupported: list[str] = Field(
        default=[],
        description=(
            "League scoring keys the board cannot honor: dd/td are per-game bonuses, and the "
            "aggregate projection and season-baseline lines the board is valued from cannot carry them"
        ),
    )


class DraftBoardResp(BaseResponse):
    data: List[DraftBoardRow]
    meta: Optional[DraftBoardMeta] = None
