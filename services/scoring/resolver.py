"""Resolve a league (or its absence) into concrete scoring strategies."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Optional

from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryDef
from services.scoring.points import PointsScoring
from services.scoring.vocab import DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS

if TYPE_CHECKING:  # pragma: no cover
    from db.models.leagues import League
    from schemas.common import LeagueInfo


@dataclass
class ResolvedScoring:
    format: str                                 # points | categories
    league: Optional["League"]
    settings_synced: bool
    point_weights: dict[str, float]             # league weights for points leagues; DEFAULT otherwise
    category_scoring: Optional[CategoryScoring] = None

    @cached_property
    def points(self) -> PointsScoring:
        """Always available: the scalar strategy (a proxy for category leagues)."""
        return PointsScoring(self.point_weights)

    @property
    def categories(self) -> Optional[CategoryScoring]:
        return self.category_scoring

    @property
    def is_categories(self) -> bool:
        return self.format == "categories"


def _league_categories(league: Optional["League"]) -> Optional[CategoryScoring]:
    """The league's own categories when it is a synced category league, else None."""
    if league is not None and league.scoring_type == "categories" and league.categories:
        cats = [CategoryDef.from_json(c) for c in league.categories]
        return CategoryScoring(cats, league.category_win_mode or "each_category")
    return None


def _league_weights(league: Optional["League"]) -> dict[str, float]:
    if league is not None and league.point_weights:
        return dict(league.point_weights)
    return dict(DEFAULT_POINT_WEIGHTS)


def resolve_scoring(league: Optional["League"], preview: Optional[str] = None) -> ResolvedScoring:
    """Resolve the league's scoring, or the team's `scoring_preview` override.

    A preview renders the team as the requested format regardless of what the
    league actually uses: `categories` on a points league uses the standard
    9-cat (ESPN/Yahoo payloads carry per-stat totals either way), `points` on
    a category league uses the league's weights or the defaults.
    """
    synced = league is not None and league.settings_synced_at is not None

    if preview == "categories":
        cats = _league_categories(league) or CategoryScoring(
            [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES], "each_category"
        )
        return ResolvedScoring("categories", league, True, _league_weights(league), cats)
    if preview == "points":
        return ResolvedScoring("points", league, True, _league_weights(league))

    if league is None:
        return ResolvedScoring("points", None, False, dict(DEFAULT_POINT_WEIGHTS))

    cats = _league_categories(league)
    if cats is not None:
        return ResolvedScoring("categories", league, synced, dict(DEFAULT_POINT_WEIGHTS), cats)
    return ResolvedScoring("points", league, synced, _league_weights(league))


def _preview_of(league_info_json: Optional[str]) -> Optional[str]:
    """`scoring_preview` from a team's serialized league_info, tolerant of old rows."""
    from services.league_service import LeagueService

    return LeagueService.preview_of(league_info_json)


def resolve_scoring_for_team(team_id: int | None) -> ResolvedScoring:
    if team_id is None:
        return resolve_scoring(None)
    from db.models.teams import Team

    team = Team.get_or_none(Team.team_id == team_id)
    league = team.league if (team is not None and team.league_id is not None) else None
    preview = _preview_of(team.league_info) if team is not None else None
    return resolve_scoring(league, preview)


def resolve_scoring_for_league_info(league_info: "LeagueInfo") -> ResolvedScoring:
    from services.league_service import LeagueService

    preview = getattr(league_info, "scoring_preview", None)
    return resolve_scoring(LeagueService.get_league_for_league_info(league_info), preview)
