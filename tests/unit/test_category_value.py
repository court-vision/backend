"""Category value: the fpts-scale scalar that stands in for avg_points in H2H-category leagues."""

import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from services.scoring.category_rank import PoolRow, compute_category_scores
from services.scoring.category_value import (
    CATEGORY_VALUE_OFFSET,
    CATEGORY_VALUE_SCALE,
    category_value,
    category_values,
    rankable_categories,
)
from services.scoring.models import CategoryDef, StatLine
from services.scoring.resolver import resolve_scoring
from services.scoring.vocab import DEFAULT_CATEGORIES

NINE_CAT = [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]


def _row(id: int, gp: int, fpts: float, **stats) -> PoolRow:
    return PoolRow(id=id, name=f"P{id}", team="DEN", gp=gp, line=StatLine.from_dict(stats),
                   fpts_avg=fpts, fpts_total=round(fpts * gp, 1))


POOL = [
    _row(1, gp=10, fpts=50.0, pts=30, reb=10, ast=8, stl=1.5, blk=1, tov=4, fg3m=3, fgm=11, fga=20, ftm=5, fta=6),
    _row(2, gp=9, fpts=40.0, pts=22, reb=6, ast=4, stl=1, blk=0.5, tov=2, fg3m=2, fgm=8, fga=16, ftm=4, fta=5),
    _row(3, gp=8, fpts=30.0, pts=15, reb=12, ast=2, stl=0.5, blk=2.5, tov=1.5, fg3m=0, fgm=6, fga=10, ftm=2, fta=4),
    _row(4, gp=2, fpts=70.0, pts=40, reb=15, ast=10, stl=3, blk=3, tov=1, fg3m=5, fgm=15, fga=25, ftm=8, fta=8),
    _row(5, gp=7, fpts=8.0, pts=4, reb=2, ast=1, stl=0.2, blk=0.1, tov=1.5, fg3m=0.5, fgm=1, fga=5, ftm=1, fta=2),
]


def _league(scoring_type="points", categories=None, weights=None):
    return SimpleNamespace(
        id=1, provider="espn", provider_league_id="123", season=2027, name="L",
        scoring_type=scoring_type, categories=categories or [], point_weights=weights or {},
        category_win_mode=None, settings_synced_at=datetime(2026, 8, 24),
        raw_settings={}, matchup_periods={}, roster_slots={},
    )


def _cat_json(*keys):
    return [{"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")} for k in keys]


@pytest.mark.unit
def test_anchors_on_the_fpts_scale():
    assert category_value(0.0) == CATEGORY_VALUE_OFFSET == 25.0          # a pool-average player
    assert category_value(15.0) == 100.0                                 # a +15 z-sum star
    assert category_value(-5.0) == 0.0                                   # the floor
    assert CATEGORY_VALUE_SCALE == 5.0


@pytest.mark.unit
def test_monotonic_in_z_and_never_negative():
    zs = [-40, -8, -5.01, -5, -4.8, -2.5, 0, 0.25, 1.234, 7.77, 15, 22]
    values = [category_value(z) for z in zs]
    assert values == sorted(values)
    assert all(v >= 0.0 for v in values)
    assert values[zs.index(-4.8)] == 1.0
    assert values[zs.index(22)] == 135.0                                 # no ceiling, only a floor


@pytest.mark.unit
def test_clamps_at_zero_without_negative_zero_and_rounds_to_a_tenth():
    for z in (-5.0, -5.02, -6.0, -100.0):
        v = category_value(z)
        assert v == 0.0 and math.copysign(1.0, v) == 1.0                 # "0.0", never "-0.0"
    assert category_value(1.234) == 31.2 == round(25 + 5 * 1.234, 1)
    assert category_value(0.123456) == 25.6
    assert category_value(-0.01) == 24.9


@pytest.mark.unit
def test_pool_values_order_exactly_like_the_category_rankings():
    scored = compute_category_scores(POOL, NINE_CAT)
    values = category_values(POOL, NINE_CAT)

    assert list(values) == [s.row.id for s in scored]                    # ranking order preserved
    for s in scored:
        value, z_sum = values[s.row.id]
        assert z_sum == s.score and value == category_value(s.score)
    ordered = [values[s.row.id][0] for s in scored]
    assert ordered == sorted(ordered, reverse=True)
    assert values[4][0] > values[1][0] > values[5][0]                    # star > solid starter > scrub
    assert all(0.0 <= v <= 100.0 for v, _ in values.values())


@pytest.mark.unit
def test_min_games_filters_the_pool_before_scoring():
    values = category_values(POOL, NINE_CAT, min_games=5)
    assert set(values) == {1, 2, 3, 5}                                   # the gp=2 row is out
    full = category_values(POOL, NINE_CAT)
    assert values[1][1] != full[1][1]                                    # z-scores are relative to the pool


@pytest.mark.unit
def test_empty_pool_and_single_player_pool():
    assert category_values([], NINE_CAT) == {}
    lone = category_values(POOL[:1], NINE_CAT)
    assert lone == {1: (CATEGORY_VALUE_OFFSET, 0.0)}                     # a degenerate pool z-scores to 0


@pytest.mark.unit
def test_rankable_categories_come_from_the_league_or_fall_back_to_nine_cat():
    eight = resolve_scoring(_league("categories", _cat_json("pts", "blk", "stl", "ast", "reb", "fg3m", "fg_pct", "ft_pct")))
    assert [c.key for c in rankable_categories(eight)] == ["pts", "blk", "stl", "ast", "reb", "fg3m", "fg_pct", "ft_pct"]

    mixed = resolve_scoring(_league("categories", _cat_json("pts", "dd", "fg_pct", "td")))
    assert [c.key for c in rankable_categories(mixed)] == ["pts", "fg_pct"]   # dd/td cannot be ranked from stored stats

    none_rankable = resolve_scoring(_league("categories", _cat_json("dd", "td")))
    assert [c.key for c in rankable_categories(none_rankable)] == DEFAULT_CATEGORIES

    preview = resolve_scoring(_league(weights={"pts": 2.0}), preview="categories")
    assert [c.key for c in rankable_categories(preview)] == DEFAULT_CATEGORIES
    assert [c.key for c in rankable_categories(resolve_scoring(None))] == DEFAULT_CATEGORIES
    assert [c.key for c in rankable_categories(None)] == DEFAULT_CATEGORIES
