"""
What a ranking says it was scored by, and what it was actually scored by.

Stored `fpts` is one hardcoded points formula, so a ranking that does not name
its basis implies a universality it does not have. These cover the `meta.scoring`
block, the league-weighted points path, and the cache fingerprint that keeps a
re-synced league from being served the old numbers.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from db import base as db_base
from schemas.common import ApiStatus
from schemas.league import LeagueDetail
from services.league_service import LeagueService
from services.rankings_service import RankingsService
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.resolver import ResolvedScoring, resolve_scoring
from services.scoring.vocab import DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS

NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in DEFAULT_CATEGORIES
]


def _league(**overrides):
    """A League-shaped row, as stored (categories are JSONB dicts)."""
    base = dict(
        id=3, provider="espn", provider_league_id="99", season=2027, name="Dunk Dynasty",
        scoring_type="points", category_win_mode=None, categories=[], point_weights={"pts": 2.0},
        matchup_periods={}, roster_slots={}, position_limits={}, draft_settings={}, raw_settings={"_sync": {"unsupported": [], "warnings": []}},
        settings_synced_at=date(2026, 8, 24), settings_synced=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _row(id: int, gp: int, **stats) -> PoolRow:
    line = StatLine.from_dict(stats)
    return PoolRow(id=id, name=f"P{id}", team="DEN", gp=gp, line=line,
                   fpts_avg=line.get("pts"), fpts_total=round(line.get("pts") * gp, 1))


# One big scorer, one big rebounder: any weighting that favours boards flips them.
POOL = [
    _row(1, gp=10, pts=30, reb=4, ast=5, tov=3),
    _row(2, gp=10, pts=12, reb=18, ast=2, tov=1),
]


@pytest.fixture(autouse=True)
def direct_db_boundary(monkeypatch):
    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_base, "run_db", direct_run_db)


@pytest.fixture
def stub_pool(monkeypatch):
    monkeypatch.setattr(RankingsService, "_load_pool",
                        staticmethod(lambda window: (date(2026, 3, 4), list(POOL))))
    monkeypatch.setattr(RankingsService, "_data_season", staticmethod(lambda: "2026-27"))


# ---- what the block says --------------------------------------------------

@pytest.mark.unit
def test_the_public_points_ranking_names_the_formula_it_used():
    basis = RankingsService._default_scoring("points")

    assert basis.basis == "default_points"
    assert basis.point_weights == dict(DEFAULT_POINT_WEIGHTS)
    assert basis.league_id is None and basis.settings_synced is None


@pytest.mark.unit
def test_a_category_ranking_has_no_point_weights():
    basis = RankingsService._default_scoring("categories")

    assert basis.basis == "categories" and basis.point_weights is None


@pytest.mark.unit
def test_a_league_points_ranking_names_the_league():
    basis = RankingsService._league_scoring(resolve_scoring(_league(point_weights={"pts": 2.0, "reb": 3.0})))

    assert basis.basis == "league_points"
    assert basis.point_weights == {"pts": 2.0, "reb": 3.0}
    assert basis.league_id == 3 and basis.league_name == "Dunk Dynasty"
    assert basis.settings_synced is True and basis.unsupported == []


@pytest.mark.unit
def test_weights_that_cannot_be_honored_are_declared_not_quietly_ignored():
    """dd/td need a per-game log; a season average cannot produce them."""
    scoring = resolve_scoring(_league(point_weights={"pts": 1.0, "dd": 5.0, "td": 10.0}))

    basis = RankingsService._league_scoring(scoring)

    assert basis.unsupported == ["dd", "td"]
    assert basis.point_weights == {"pts": 1.0}      # reports what was applied, not what was asked


@pytest.mark.unit
def test_unreadable_league_settings_are_flagged_rather_than_silently_defaulted():
    scoring = resolve_scoring(_league(point_weights={}, settings_synced=False, settings_synced_at=None))

    basis = RankingsService._league_scoring(scoring)

    assert basis.settings_synced is False
    assert basis.point_weights == dict(DEFAULT_POINT_WEIGHTS)   # the substitute, named as such


@pytest.mark.unit
def test_a_team_with_no_league_reports_the_platform_default():
    basis = RankingsService._league_scoring(resolve_scoring(None))

    assert basis.basis == "default_points" and basis.league_id is None


# ---- what it actually scored ----------------------------------------------

@pytest.mark.unit
def test_league_weights_change_the_order_not_just_the_label(stub_pool):
    """The scorer leads under default weights; the rebounder leads when boards pay."""
    scoring = ResolvedScoring("points", _league(), True, {"pts": 1.0, "reb": 5.0})

    resp = asyncio.run(RankingsService.get_league_rankings(scoring))

    assert resp.status == ApiStatus.SUCCESS
    assert [p.id for p in resp.data] == [2, 1]
    top = resp.data[0]
    assert top.avg_fpts == pytest.approx(12 + 18 * 5)      # 102 per game
    assert top.total_fpts == pytest.approx(102 * 10)       # scaled by games played
    assert resp.meta.scoring.basis == "league_points"


@pytest.mark.unit
def test_default_weights_take_the_stored_path_rather_than_recomputing(monkeypatch):
    """Every stored fpts column was computed with exactly these weights."""
    called = []

    async def fake_season(basis):
        called.append(basis)
        return "season"

    monkeypatch.setattr(RankingsService, "_get_season_rankings", staticmethod(fake_season))
    scoring = ResolvedScoring("points", _league(), True, dict(DEFAULT_POINT_WEIGHTS))

    async def run():
        return await RankingsService.get_league_rankings(scoring)

    assert asyncio.run(run()) == "season"
    assert called[0].basis == "league_points"


@pytest.mark.unit
def test_a_category_league_is_ranked_on_its_own_categories(stub_pool):
    scoring = resolve_scoring(_league(scoring_type="categories", categories=NINE_CAT[:3],
                                      category_win_mode="each_category"))

    resp = asyncio.run(RankingsService.get_league_rankings(scoring))

    assert resp.meta.format == "categories"
    assert [c.key for c in resp.meta.categories] == [c["key"] for c in NINE_CAT[:3]]
    assert resp.meta.scoring.basis == "categories"
    assert resp.meta.scoring.league_name == "Dunk Dynasty"


@pytest.mark.unit
def test_an_invalid_window_is_a_bad_request_not_a_500(stub_pool):
    resp = asyncio.run(RankingsService.get_league_rankings(resolve_scoring(_league()), window=12))

    assert resp.status == ApiStatus.BAD_REQUEST and resp.data == []


@pytest.mark.unit
def test_min_games_filters_the_league_points_pool(stub_pool):
    scoring = ResolvedScoring("points", _league(), True, {"pts": 1.0})

    resp = asyncio.run(RankingsService.get_league_rankings(scoring, min_games=50))

    assert resp.data == [] and "50+ games" in resp.message


# ---- the cache fingerprint -------------------------------------------------

@pytest.mark.unit
def test_the_fingerprint_is_stable_for_equivalent_scoring():
    a = ResolvedScoring("points", _league(), True, {"pts": 1.0, "reb": 2.0})
    b = ResolvedScoring("points", _league(id=99, name="Elsewhere"), True, {"reb": 2.0, "pts": 1.0})

    assert a.fingerprint == b.fingerprint      # weight order is not identity


@pytest.mark.unit
def test_the_fingerprint_moves_when_the_settings_do():
    before = resolve_scoring(_league(point_weights={"pts": 1.0}))
    after = resolve_scoring(_league(point_weights={"pts": 3.0}))
    categories = resolve_scoring(_league(scoring_type="categories", categories=NINE_CAT))

    assert before.fingerprint != after.fingerprint
    assert categories.fingerprint != before.fingerprint
    assert categories.fingerprint[0] == "categories"


# ---- the LeagueDetail seam -------------------------------------------------

@pytest.mark.unit
def test_resolving_from_the_auth_context_matches_resolving_from_the_row():
    """`get_owned_team` hands over a LeagueDetail, whose categories are models
    rather than dicts. Both must resolve identically, or the request path and
    every other caller silently disagree about what a league scores."""
    row = _league(scoring_type="categories", categories=NINE_CAT, category_win_mode="most_categories")
    detail = LeagueService.to_detail(row)
    assert isinstance(detail, LeagueDetail)

    from_row = resolve_scoring(row)
    from_detail = resolve_scoring(detail)

    assert from_detail.format == from_row.format == "categories"
    assert from_detail.categories.keys == from_row.categories.keys
    assert from_detail.categories.win_mode == from_row.categories.win_mode == "most_categories"
    assert from_detail.fingerprint == from_row.fingerprint
    assert from_detail.settings_synced == from_row.settings_synced


@pytest.mark.unit
def test_a_points_league_resolves_the_same_from_either_shape():
    row = _league(point_weights={"pts": 2.5, "blk": 6.0})
    detail = LeagueService.to_detail(row)

    assert resolve_scoring(detail).fingerprint == resolve_scoring(row).fingerprint
    assert resolve_scoring(detail).points.weights == {"pts": 2.5, "blk": 6.0}
