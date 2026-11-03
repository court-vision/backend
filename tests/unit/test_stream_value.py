"""
services.stream_value: what a player is worth per game to a stream plan under the
question asked -- one category's rate, or the category value re-weighted toward the
matchup's close categories.
"""

import pytest

from services.scoring.category_rank import PoolRow
from services.scoring.category_value import category_value
from services.scoring.models import StatLine
from services.stream_value import (
    STREAMABLE_CATEGORIES,
    VALUE_KINDS,
    VALUE_LABELS,
    category_rate,
    category_weight,
    matchup_weights,
    pool_stds,
    signed_margin,
    swing,
    weighted_category_value,
)


def row(pid, **stats):
    return PoolRow(id=pid, name=f"P{pid}", team=None, gp=10, line=StatLine(**stats), fpts_avg=0.0, fpts_total=0.0)


@pytest.mark.unit
def test_every_kind_has_a_label_and_only_counting_categories_are_streamable():
    assert set(VALUE_KINDS) == set(VALUE_LABELS)
    assert "fg_pct" not in STREAMABLE_CATEGORIES and "tov" not in STREAMABLE_CATEGORIES
    assert set(STREAMABLE_CATEGORIES) <= set(VALUE_KINDS)


@pytest.mark.unit
def test_a_category_rate_is_the_lines_per_game_figure():
    assert category_rate(StatLine(blk=2.5), "blk") == 2.5
    assert category_rate(StatLine(), "blk") == 0.0 and category_rate(None, "blk") == 0.0


@pytest.mark.unit
def test_the_weight_falls_with_the_margin_against_the_swing():
    assert category_weight(0.0, 10.0) == 1.0
    assert category_weight(10.0, 10.0) == 0.5 and category_weight(-10.0, 10.0) == 0.5
    assert category_weight(30.0, 10.0) == pytest.approx(0.1)
    assert swing(2.0, 3.0) == 12.0 and swing(0.0, 3.0) > 0            # never zero


@pytest.mark.unit
def test_a_signed_margin_is_positive_when_you_lead():
    assert signed_margin(50, 40, True) == 10 and signed_margin(50, 40, False) == -10


@pytest.mark.unit
def test_pool_stds_and_matchup_weights():
    pool = [row(1, blk=1.0, pts=10), row(2, blk=3.0, pts=30)]
    stds = pool_stds(pool, ["blk", "pts"])
    assert stds == {"blk": 1.0, "pts": 10.0}
    # Three games for a spot: a blocks swing is 2 * 1 * 3 = 6, a points swing 60.
    weights = matchup_weights({"blk": 6.0, "pts": 0.0}, stds, 3.0)
    assert weights == {"blk": 0.5, "pts": 1.0}
    assert pool_stds([row(1, blk=1.0)], ["blk"]) == {"blk": 0.0}


@pytest.mark.unit
def test_the_weighted_value_is_the_proxy_when_every_category_counts_alike():
    z = {"pts": 1.0, "reb": -0.5, "blk": 2.0}
    assert weighted_category_value(z, {"pts": 1.0, "reb": 1.0, "blk": 1.0}) == category_value(2.5)
    assert weighted_category_value(z, {"pts": 0.3, "reb": 0.3, "blk": 0.3}) == category_value(2.5)


@pytest.mark.unit
def test_one_category_in_play_makes_the_value_that_categorys_standing():
    z = {"pts": 1.0, "reb": -0.5, "blk": 2.0}
    assert weighted_category_value(z, {"pts": 0.0, "reb": 0.0, "blk": 1.0}) == category_value(2.0 * 3)
    assert weighted_category_value({"pts": 1.0, "blk": -1.0}, {"pts": 0.0, "blk": 1.0}) == category_value(-2.0)


@pytest.mark.unit
def test_nothing_in_play_is_the_pool_average():
    assert weighted_category_value({"pts": 3.0}, {"pts": 0.0}) == 25.0
    assert weighted_category_value({"pts": 3.0}, {}) == 25.0
