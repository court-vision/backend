"""
Scoring engine unit tests: canonical vocabulary, points strategy, category strategy.
"""

import pytest

from services.scoring import (
    DEFAULT_CATEGORIES,
    DEFAULT_POINT_WEIGHTS,
    DEFAULT_POINTS,
    ESPN_ID_TO_KEY,
    STATS,
    YAHOO_ID_TO_KEY,
    CategoryDef,
    CategoryScoring,
    CategoryTeamScoreData,
    PointsScoring,
    StatLine,
)

# The data-platform transformer's canonical test vector (tests/unit/test_transformers.py) -> 49 fpts
VECTOR_49 = dict(pts=20, reb=10, ast=5, stl=2, blk=1, tov=3, fgm=8, fga=15, fg3m=3, ftm=5, fta=6)


@pytest.mark.unit
def test_default_weights_reproduce_hardcoded_formula():
    assert DEFAULT_POINTS.score(StatLine.from_dict(VECTOR_49)) == 49
    assert DEFAULT_POINTS.is_default


@pytest.mark.unit
def test_provider_id_maps_are_injective_and_cover_nine_cat():
    assert len(ESPN_ID_TO_KEY) == len({d.espn_id for d in STATS.values() if d.espn_id is not None})
    assert len(YAHOO_ID_TO_KEY) == len({d.yahoo_id for d in STATS.values() if d.yahoo_id is not None})
    for key in DEFAULT_CATEGORIES:
        assert STATS[key].espn_id is not None and STATS[key].yahoo_id is not None
    assert STATS["tov"].higher_is_better is False
    assert STATS["fg_pct"].is_rate and STATS["fg_pct"].numerator == "fgm"


@pytest.mark.unit
def test_points_scoring_is_linear_so_averages_score_directly():
    weights = {"pts": 1.5, "reb": 1.2, "tov": -1.0}
    scorer = PointsScoring(weights)
    games = [StatLine(pts=20, reb=5, tov=2), StatLine(pts=30, reb=9, tov=4), StatLine(pts=10, reb=1, tov=0)]
    avg = StatLine.sum(games).scaled(1 / 3)
    assert scorer.score(avg) == pytest.approx(sum(scorer.score(g) for g in games) / 3)
    assert not scorer.is_default


@pytest.mark.unit
def test_points_scoring_ignores_rate_keys_and_flags_game_only_stats():
    scorer = PointsScoring({"pts": 1, "fg_pct": 10, "dd": 5})
    assert "fg_pct" not in scorer.weights
    assert scorer.uses_game_only_stats
    line = StatLine.from_game_row(type("Row", (), dict(pts=12, reb=11, ast=10, stl=1, blk=0, tov=2,
                                                        fgm=5, fga=9, fg3m=1, fg3a=2, ftm=1, fta=2, min=30))())
    assert line.dd == 1.0 and line.td == 1.0
    assert scorer.score(line) == 12 + 5


def _nine_cat() -> CategoryScoring:
    return CategoryScoring([CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES])


@pytest.mark.unit
def test_category_rates_come_from_summed_makes_and_attempts():
    cs = _nine_cat()
    totals = cs.team_totals([StatLine(fgm=1, fga=2, ftm=0, fta=0), StatLine(fgm=9, fga=10, ftm=3, fta=4)])
    assert totals["fg_pct"] == pytest.approx(10 / 12, abs=1e-4)      # not the mean of 0.5 and 0.9
    assert totals["ft_pct"] == pytest.approx(0.75, abs=1e-4)


@pytest.mark.unit
def test_category_compare_inverts_turnovers_and_counts_record():
    cs = _nine_cat()
    you = {"fg_pct": 0.48, "ft_pct": 0.80, "fg3m": 40, "pts": 500, "reb": 200, "ast": 100, "stl": 30, "blk": 20, "tov": 50}
    opp = {"fg_pct": 0.48, "ft_pct": 0.79, "fg3m": 45, "pts": 480, "reb": 200, "ast": 90, "stl": 30, "blk": 25, "tov": 60}
    cmp = cs.compare(you, opp)
    by_key = {i.key: i.winner for i in cmp.items}
    assert by_key["tov"] == "you"          # fewer turnovers wins
    assert by_key["fg_pct"] == "tie" and by_key["reb"] == "tie" and by_key["stl"] == "tie"
    assert by_key["blk"] == "opp" and by_key["fg3m"] == "opp"
    assert (cmp.wins, cmp.losses, cmp.ties) == (4, 2, 3)
    assert cs.week_won(cmp) is True
    assert cs.week_won(cs.compare(you, you)) is None


@pytest.mark.unit
def test_category_overlay_recomputes_rates_only_with_raw_totals():
    cs = _nine_cat()
    base_no_raw = CategoryTeamScoreData(totals={"pts": 100, "fg_pct": 0.5}, raw=None)
    out = cs.overlay(base_no_raw, StatLine(pts=20, fgm=10, fga=10))
    assert out.totals["pts"] == 120 and out.totals["fg_pct"] == 0.5 and out.live_adjusted

    base_raw = CategoryTeamScoreData(totals={"pts": 100, "fg_pct": 0.5}, raw={"fgm": 50, "fga": 100})
    out = cs.overlay(base_raw, StatLine(pts=20, fgm=10, fga=10))
    assert out.totals["fg_pct"] == pytest.approx(60 / 110, abs=1e-4)
    assert out.raw["fga"] == 110


@pytest.mark.unit
def test_category_projection_scales_averages_by_games_and_skips_uncounted():
    cs = _nine_cat()
    base = CategoryTeamScoreData(totals={"pts": 100, "reb": 40, "fg_pct": 0.5}, raw={"fgm": 50, "fga": 100})
    roster = [
        (StatLine(pts=20, reb=5, fgm=8, fga=16), 3, True),   # counted: 3 games left
        (StatLine(pts=30, reb=10, fgm=10, fga=20), 2, False),  # injured / IR: excluded
    ]
    proj = cs.project(base, roster)
    assert proj["pts"] == 160 and proj["reb"] == 55
    assert proj["fg_pct"] == pytest.approx((50 + 24) / (100 + 48), abs=1e-4)
