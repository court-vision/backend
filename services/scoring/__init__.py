"""Scoring engine: canonical stats, points and category strategies, and league resolution."""

from services.scoring.categories import CategoryScoring
from services.scoring.category_value import (
    CATEGORY_VALUE_OFFSET,
    CATEGORY_VALUE_SCALE,
    category_value,
    category_values,
    rankable_categories,
)
from services.scoring.models import (
    CategoryComparisonData,
    CategoryDef,
    CategoryItemResult,
    CategoryTeamScoreData,
    LeagueSettings,
    StatLine,
)
from services.scoring.points import DEFAULT_POINTS, PointsScoring
from services.scoring.resolver import (
    ResolvedScoring,
    resolve_scoring,
    resolve_scoring_for_league_info,
    resolve_scoring_for_team,
)
from services.scoring.vocab import (
    DEFAULT_CATEGORIES,
    DEFAULT_POINT_WEIGHTS,
    ESPN_ID_TO_KEY,
    STATS,
    YAHOO_ID_TO_KEY,
)

__all__ = [
    "CATEGORY_VALUE_OFFSET", "CATEGORY_VALUE_SCALE", "category_value", "category_values", "rankable_categories",
    "CategoryScoring", "CategoryComparisonData", "CategoryDef", "CategoryItemResult",
    "CategoryTeamScoreData", "LeagueSettings", "StatLine", "DEFAULT_POINTS", "PointsScoring",
    "ResolvedScoring", "resolve_scoring", "resolve_scoring_for_league_info", "resolve_scoring_for_team",
    "DEFAULT_CATEGORIES", "DEFAULT_POINT_WEIGHTS", "ESPN_ID_TO_KEY", "STATS", "YAHOO_ID_TO_KEY",
]
