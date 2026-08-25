"""A team's `scoring_preview` overrides how its league is rendered, without touching the league."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from schemas.league import LeagueSummary
from services.league_service import LeagueService
from services.scoring import DEFAULT_POINT_WEIGHTS
from services.scoring.resolver import _preview_of, resolve_scoring
from services.scoring.vocab import DEFAULT_CATEGORIES
from services.team_service import TeamService

NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in DEFAULT_CATEGORIES
]


def _league(scoring_type="points", categories=None, weights=None, synced=True, win_mode=None):
    return SimpleNamespace(
        id=1, provider="espn", provider_league_id="123", season=2026, name="L",
        scoring_type=scoring_type, categories=categories or [], point_weights=weights or {},
        category_win_mode=win_mode, settings_synced_at=datetime(2026, 8, 24) if synced else None,
        raw_settings={}, matchup_periods={}, roster_slots={},
    )


@pytest.mark.unit
def test_no_preview_resolves_the_league_as_before():
    assert resolve_scoring(_league()).format == "points"
    assert resolve_scoring(_league("categories", NINE_CAT)).format == "categories"
    assert resolve_scoring(None).format == "points"


@pytest.mark.unit
def test_categories_preview_on_a_points_league_uses_standard_nine_cat():
    s = resolve_scoring(_league(weights={"pts": 2.0}), preview="categories")
    assert s.is_categories and s.settings_synced
    assert [c.key for c in s.categories.categories] == DEFAULT_CATEGORIES
    assert s.categories.win_mode == "each_category"
    assert s.point_weights == {"pts": 2.0}          # scalar proxy keeps the league's weights


@pytest.mark.unit
def test_categories_preview_on_a_category_league_keeps_its_own_categories():
    eight = NINE_CAT[:-1]
    s = resolve_scoring(_league("categories", eight, win_mode="most_categories"), preview="categories")
    assert [c.key for c in s.categories.categories] == [c["key"] for c in eight]
    assert s.categories.win_mode == "most_categories"


@pytest.mark.unit
def test_points_preview_on_a_category_league_and_on_no_league():
    s = resolve_scoring(_league("categories", NINE_CAT), preview="points")
    assert s.format == "points" and s.point_weights == DEFAULT_POINT_WEIGHTS and s.categories is None
    assert resolve_scoring(None, preview="categories").is_categories
    assert resolve_scoring(None, preview="points").format == "points"


@pytest.mark.unit
def test_preview_is_read_tolerantly_from_stored_league_info():
    assert _preview_of(json.dumps({"scoring_preview": "categories"})) == "categories"
    assert _preview_of(json.dumps({"scoring_preview": "roto"})) is None
    assert _preview_of(json.dumps({"team_name": "x"})) is None
    assert _preview_of(None) is None and _preview_of("not json") is None


@pytest.mark.unit
def test_league_info_round_trips_the_preview_and_defaults_to_none():
    li = LeagueInfo(provider=FantasyProvider.ESPN, league_id=5, team_name="T", year=2026, scoring_preview="categories")
    stored = json.loads(TeamService.serialize_league_info(li))
    assert stored["scoring_preview"] == "categories"
    assert TeamService.deserialize_league_info(stored).scoring_preview == "categories"
    assert TeamService.deserialize_league_info({"league_id": 5, "team_name": "T", "year": 2026}).scoring_preview is None
    with pytest.raises(ValueError):
        LeagueInfo(league_id=5, team_name="T", year=2026, scoring_preview="roto")


@pytest.mark.unit
def test_summary_for_team_overlays_the_preview_without_touching_the_league():
    league = _league(weights=dict(DEFAULT_POINT_WEIGHTS))
    li = LeagueInfo(league_id=123, team_name="T", year=2026, scoring_preview="categories")
    summary = LeagueService.summary_for_team(league, li)
    assert summary.scoring_type == "categories" and summary.scoring_preview == "categories"
    assert [c.key for c in summary.categories] == DEFAULT_CATEGORIES and summary.settings_synced
    assert summary.point_weights == DEFAULT_POINT_WEIGHTS       # still reported for the scalar proxy
    assert league.scoring_type == "points"                     # the league row is untouched

    plain = LeagueService.summary_for_team(league, LeagueInfo(league_id=123, team_name="T", year=2026))
    assert plain.scoring_type == "points" and plain.scoring_preview is None

    # A team whose league has not synced yet still gets a usable summary
    synthetic = LeagueService.summary_for_team(None, li)
    assert isinstance(synthetic, LeagueSummary) and synthetic.scoring_type == "categories" and synthetic.id == 0
    assert LeagueService.summary_for_team(None, LeagueInfo(league_id=123, team_name="T", year=2026)) is None


@pytest.mark.unit
def test_detail_honors_preview_read_straight_from_stored_json():
    league = _league(weights=dict(DEFAULT_POINT_WEIGHTS))
    assert LeagueService.preview_of('{"scoring_preview": "categories"}') == "categories"
    assert LeagueService.preview_of("{}") is None and LeagueService.preview_of("") is None
    detail = LeagueService.to_detail(league, LeagueService.preview_of('{"scoring_preview": "categories"}'))
    assert detail.scoring_type == "categories" and detail.scoring_preview == "categories"
    assert [c.key for c in detail.categories] == DEFAULT_CATEGORIES
    assert LeagueService.to_detail(league, None).scoring_type == "points"
