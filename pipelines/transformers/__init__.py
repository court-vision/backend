"""Shim over cv_core.transformers — the shared implementation lives in cv-core.

The fantasy-points formula that lived here was one of five hand-written copies
across two repos and had no backend caller at all; the one copy is now
cv_core.transformers.fantasy_points, derived from the scoring vocabulary's
DEFAULT_POINT_WEIGHTS.
"""

from cv_core.transformers import (  # noqa: F401
    PlayerStats,
    calculate_fantasy_points,
    minutes_to_int,
    normalize_name,
)

__all__ = [
    "PlayerStats",
    "normalize_name",
    "calculate_fantasy_points",
    "minutes_to_int",
]
