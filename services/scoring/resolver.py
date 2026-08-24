"""Resolve a league (or its absence) into concrete scoring strategies."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Optional

from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryDef
from services.scoring.points import PointsScoring
from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

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


def resolve_scoring(league: Optional["League"]) -> ResolvedScoring:
    if league is None:
        return ResolvedScoring("points", None, False, dict(DEFAULT_POINT_WEIGHTS))

    synced = league.settings_synced_at is not None
    if league.scoring_type == "categories" and league.categories:
        cats = [CategoryDef.from_json(c) for c in league.categories]
        return ResolvedScoring(
            "categories", league, synced, dict(DEFAULT_POINT_WEIGHTS),
            CategoryScoring(cats, league.category_win_mode or "each_category"),
        )

    weights = dict(league.point_weights) if league.point_weights else dict(DEFAULT_POINT_WEIGHTS)
    return ResolvedScoring("points", league, synced, weights)


def resolve_scoring_for_team(team_id: int | None) -> ResolvedScoring:
    if team_id is None:
        return resolve_scoring(None)
    from db.models.teams import Team

    team = Team.get_or_none(Team.team_id == team_id)
    league = team.league if (team is not None and team.league_id is not None) else None
    return resolve_scoring(league)


def resolve_scoring_for_league_info(league_info: "LeagueInfo") -> ResolvedScoring:
    from services.league_service import LeagueService

    return resolve_scoring(LeagueService.get_league_for_league_info(league_info))
