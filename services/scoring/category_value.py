"""
Category value: a non-negative, fpts-scale scalar for H2H-category leagues.

The lineup optimizer and the streamer finder rank players by one number
(`avg_points`) and assume it behaves like fantasy points: >= 0, additive over
games, roughly 0-100. For category leagues that number is the player's summed
per-category z-score (services.scoring.category_rank) mapped onto that scale:

    value = max(0, OFFSET + SCALE * z_sum)

so a pool-average player is worth ~25, a +15 z-sum star ~100, and anyone at
or below -5 is worth 0. Ordering is exactly the category-rankings ordering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from services.scoring.category_rank import RANKABLE_KEYS, PoolRow, compute_category_scores
from services.scoring.models import CategoryDef
from services.scoring.vocab import DEFAULT_CATEGORIES

if TYPE_CHECKING:  # pragma: no cover
    from services.scoring.resolver import ResolvedScoring

CATEGORY_VALUE_OFFSET = 25.0
CATEGORY_VALUE_SCALE = 5.0
VALUE_DECIMALS = 1


def category_value(z_sum: float) -> float:
    """Map a summed z-score onto the fpts-like scale, clamped at zero."""
    return max(0.0, round(CATEGORY_VALUE_OFFSET + CATEGORY_VALUE_SCALE * z_sum, VALUE_DECIMALS))


def category_values(pool: list[PoolRow], cat_defs: list[CategoryDef],
                    min_games: int = 1) -> dict[int, tuple[float, float]]:
    """Player id -> (category value, z_sum) for every pool row with gp >= min_games."""
    eligible = [row for row in pool if row.gp >= min_games]
    return {s.row.id: (category_value(s.score), s.score) for s in compute_category_scores(eligible, cat_defs)}


def default_category_defs() -> list[CategoryDef]:
    return [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]


def rankable_categories(scoring: Optional["ResolvedScoring"]) -> list[CategoryDef]:
    """The league's categories that can be ranked from stored stats (standard 9-cat when none can)."""
    cats = scoring.categories.categories if (scoring is not None and scoring.categories is not None) else []
    rankable = [c for c in cats if c.key in RANKABLE_KEYS]
    return rankable or default_category_defs()
