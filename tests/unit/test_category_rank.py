"""
Category rankings: per-category z-scores over a pool, rate impact, polarity, ordering.
"""

import pytest

from services.scoring.category_rank import RANKABLE_KEYS, PoolRow, compute_category_scores
from services.scoring.models import CategoryDef, StatLine
from services.scoring.vocab import DEFAULT_CATEGORIES


def _row(id: int, fpts: float = 0.0, gp: int = 10, **stats) -> PoolRow:
    return PoolRow(id=id, name=f"P{id}", team="DEN", gp=gp, line=StatLine.from_dict(stats),
                   fpts_avg=fpts, fpts_total=fpts * gp)


def _cats(*keys: str) -> list[CategoryDef]:
    return [CategoryDef.for_key(k) for k in keys]


def _by_id(scored) -> dict:
    return {s.row.id: s for s in scored}


@pytest.mark.unit
def test_rankable_keys_cover_standard_nine_cat():
    assert set(DEFAULT_CATEGORIES) <= set(RANKABLE_KEYS)
    assert "min" not in RANKABLE_KEYS and "dd" not in RANKABLE_KEYS


@pytest.mark.unit
def test_turnovers_are_inverted_so_high_tov_is_negative():
    scored = _by_id(compute_category_scores([_row(1, tov=5), _row(2, tov=2), _row(3, tov=1)], _cats("tov")))
    assert scored[1].z["tov"] < 0 < scored[3].z["tov"]
    assert scored[1].values["tov"] == 5.0          # display value is the raw per-game number
    assert scored[1].score == scored[1].z["tov"]


@pytest.mark.unit
def test_rate_impact_rewards_volume_over_raw_percentage():
    # Pool FG% = 36.2 / 76 ~ 47.6%. A 55% shooter on 20 FGA/g adds ~1.5 makes/g over a
    # pool-average shooter at his volume; a 60% shooter on 2 FGA/g adds ~0.25.
    pool = [
        _row(1, fgm=11.0, fga=20.0),   # 55%
        _row(2, fgm=1.2, fga=2.0),     # 60%
        _row(3, fgm=8.0, fga=20.0),    # 40%
        _row(4, fgm=9.0, fga=18.0),    # 50%
        _row(5, fgm=7.0, fga=16.0),    # 43.75%
        _row(6, fgm=0.0, fga=0.0),     # no attempts
    ]
    scored = compute_category_scores(pool, _cats("fg_pct"))
    by_id = _by_id(scored)

    assert by_id[1].z["fg_pct"] > by_id[2].z["fg_pct"] > 0
    assert scored[0].row.id == 1
    # Display value is the player's own percentage, as a 0-1 fraction
    assert by_id[1].values["fg_pct"] == pytest.approx(0.55)
    assert by_id[2].values["fg_pct"] == pytest.approx(0.60)
    # No attempts: no percentage to show, zero impact (never a NaN or a penalty)
    assert by_id[6].values["fg_pct"] is None
    assert by_id[6].z["fg_pct"] == pytest.approx(0.0, abs=0.05)


@pytest.mark.unit
def test_zero_spread_gives_zero_z_for_everyone():
    scored = compute_category_scores([_row(1, pts=20.1), _row(2, pts=20.1), _row(3, pts=20.1)], _cats("pts"))
    assert all(s.z["pts"] == 0.0 and s.score == 0.0 for s in scored)
    assert str(scored[0].z["pts"]) == "0.0"       # never a negative zero in output


@pytest.mark.unit
def test_ordering_by_score_then_fpts_tiebreak():
    pool = [
        _row(1, fpts=30.0, pts=10, reb=5),
        _row(2, fpts=45.0, pts=10, reb=5),         # identical stats to 1, higher fpts wins the tie
        _row(3, fpts=20.0, pts=30, reb=12),        # best in both categories
        _row(4, fpts=60.0, pts=5, reb=2),          # worst in both, fpts does not rescue it
    ]
    scored = compute_category_scores(pool, _cats("pts", "reb"))
    assert [s.row.id for s in scored] == [3, 2, 1, 4]
    assert scored[1].score == scored[2].score
    for s in scored:
        assert s.score == pytest.approx(sum(s.z.values()), abs=2e-3)
        assert set(s.values) == {"pts", "reb"} and set(s.z) == {"pts", "reb"}


@pytest.mark.unit
def test_z_scores_are_population_z_rounded_to_three_decimals():
    scored = _by_id(compute_category_scores([_row(1, ast=2), _row(2, ast=4), _row(3, ast=9)], _cats("ast")))
    # mean 5, population std sqrt(26/3)
    std = (26 / 3) ** 0.5
    assert scored[3].z["ast"] == round((9 - 5) / std, 3)
    assert scored[1].z["ast"] == round((2 - 5) / std, 3)


@pytest.mark.unit
@pytest.mark.parametrize("key", ["min", "dd", "ato", "bogus"])
def test_unsupported_category_raises(key):
    cat = CategoryDef.for_key(key) if key != "bogus" else CategoryDef(key="bogus", label="?")
    with pytest.raises(ValueError, match=key):
        compute_category_scores([_row(1, pts=1)], [cat])


@pytest.mark.unit
def test_empty_pool_returns_empty():
    assert compute_category_scores([], _cats("pts")) == []
