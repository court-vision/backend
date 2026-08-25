"""
`PlayerValueService.avg_points_for`: one dispatcher for every provider's avg_points.
Category leagues (including a scoring_preview) get the category value over the
rolling pool (last season's baseline pool before opening night); points leagues
get fantasy points under the league's weights. No DB: the pool loaders are stubbed.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from services import player_value_service as pvs
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.category_rank import PoolRow, compute_category_scores
from services.scoring.category_value import category_value
from services.scoring.models import CategoryDef, StatLine
from services.scoring.points import PointsScoring
from services.scoring.resolver import ResolvedScoring, resolve_scoring
from services.scoring.vocab import DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS

NINE_CAT = [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]


def _row(id: int, espn_id: int, name: str, team: str, gp: int, **stats) -> PoolRow:
    return PoolRow(id=id, name=name, team=team, gp=gp, line=StatLine.from_dict(stats),
                   fpts_avg=0.0, fpts_total=0.0, espn_id=espn_id, name_normalized=name.lower())


ROLLING = [
    _row(101, 3112335, "Nikola Jokić", "DEN", 6, pts=28, reb=12, ast=10, stl=1.5, blk=0.8, tov=3, fg3m=1.5, fgm=11, fga=19, ftm=5, fta=6),
    _row(102, 4066648, "Role Player", "LAL", 6, pts=10, reb=4, ast=2, stl=0.6, blk=0.3, tov=1, fg3m=1, fgm=4, fga=9, ftm=1, fta=1.5),
    _row(103, 5550001, "Twin Name", "BOS", 5, pts=18, reb=6, ast=5, stl=1, blk=0.5, tov=2, fg3m=2, fgm=7, fga=15, ftm=2, fta=3),
    _row(104, 5550002, "Twin Name", "MIA", 5, pts=5, reb=2, ast=1, stl=0.2, blk=0.1, tov=1, fg3m=0.5, fgm=2, fga=6, ftm=0.5, fta=1),
    _row(105, 5550003, "Scrub", "UTA", 4, pts=2, reb=1, ast=0.5, stl=0.1, blk=0, tov=1.5, fg3m=0, fgm=1, fga=4, ftm=0, fta=0),
]
BASELINE = [
    _row(101, 3112335, "Nikola Jokić", "DEN", 70, pts=26, reb=12, ast=9, stl=1.3, blk=0.7, tov=3, fg3m=1.2, fgm=10, fga=17, ftm=4.5, fta=5.5),
    _row(102, 4066648, "Role Player", "LAL", 60, pts=9, reb=4, ast=2, stl=0.5, blk=0.3, tov=1, fg3m=1, fgm=3.5, fga=8, ftm=1, fta=1.5),
    _row(105, 5550003, "Scrub", "UTA", 30, pts=3, reb=1, ast=0.5, stl=0.1, blk=0, tov=1, fg3m=0.2, fgm=1, fga=3, ftm=0.5, fta=1),
]


def _league(scoring_type="points", categories=None, weights=None):
    return SimpleNamespace(
        id=1, provider="espn", provider_league_id="123", season=2027, name="L",
        scoring_type=scoring_type, categories=categories or [], point_weights=weights or {},
        category_win_mode=None, settings_synced_at=datetime(2026, 8, 24),
        raw_settings={}, matchup_periods={}, roster_slots={},
    )


NINE_CAT_JSON = [{"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
                 for k in DEFAULT_CATEGORIES]


@pytest.fixture
def pools(monkeypatch):
    """Stub the pool loaders; `calls` records which windows were requested."""
    calls = {"rolling": [], "baseline": 0}
    state = {"rolling": list(ROLLING), "baseline": list(BASELINE)}

    def fake_load_pool(window, season=None):
        calls["rolling"].append(window)
        return (date(2026, 11, 10) if state["rolling"] else None), list(state["rolling"])

    def fake_baseline(season=None):
        calls["baseline"] += 1
        return list(state["baseline"])

    monkeypatch.setattr(pvs, "load_pool", fake_load_pool)
    monkeypatch.setattr(pvs, "load_baseline_pool", fake_baseline)
    return SimpleNamespace(calls=calls, state=state)


def _expected_values(pool, cats):
    return {s.row.id: category_value(s.score) for s in compute_category_scores(pool, cats)}


# ---- category leagues ---------------------------------------------------------------


@pytest.mark.unit
def test_category_league_gets_category_values_keyed_by_espn_id(pools):
    scoring = resolve_scoring(_league("categories", NINE_CAT_JSON))
    assert PlayerValueService.value_kind_for(scoring) == "cat_value"

    out = PlayerValueService.avg_points_for(scoring, espn_ids=[3112335, 4066648, 5550003, 999])

    expected = _expected_values(ROLLING, NINE_CAT)
    assert out[3112335] == ValueResult(expected[101], "rolling")
    assert out[4066648] == ValueResult(expected[102], "rolling")
    assert out[5550003] == ValueResult(expected[105], "rolling")
    assert out[999] == ValueResult(None, None)                           # not in the pool
    assert out[3112335].value > out[4066648].value > out[5550003].value
    assert all(0.0 <= r.value <= 100.0 for r in out.values() if r.value is not None)
    assert pools.calls["rolling"] == [14] and pools.calls["baseline"] == 0


@pytest.mark.unit
def test_category_league_uses_only_its_rankable_categories(pools):
    eight = [c for c in NINE_CAT_JSON if c["key"] != "tov"] + [{"key": "dd", "label": "DD", "higher_is_better": True, "is_rate": False}]
    scoring = resolve_scoring(_league("categories", eight))
    out = PlayerValueService.avg_points_for(scoring, espn_ids=[3112335, 5550003])

    expected = _expected_values(ROLLING, [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES if k != "tov"])
    assert out[3112335].value == expected[101] and out[5550003].value == expected[105]
    assert out[3112335].value != _expected_values(ROLLING, NINE_CAT)[101]


@pytest.mark.unit
def test_scoring_preview_categories_on_a_points_league_takes_the_category_path(pools, monkeypatch):
    seen = {}
    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id",
                        staticmethod(lambda *a, **k: seen.setdefault("points_path", True) and {}))

    scoring = resolve_scoring(_league(weights={"pts": 2.0}), preview="categories")
    assert scoring.is_categories and PlayerValueService.value_kind_for(scoring) == "cat_value"

    out = PlayerValueService.avg_points_for(scoring, espn_ids=[3112335])
    assert out[3112335] == ValueResult(_expected_values(ROLLING, NINE_CAT)[101], "rolling")
    assert "points_path" not in seen


@pytest.mark.unit
def test_category_value_falls_back_to_last_seasons_baseline_pool(pools):
    pools.state["rolling"] = []                                          # August: no fresh snapshot
    scoring = resolve_scoring(_league("categories", NINE_CAT_JSON))

    out = PlayerValueService.avg_points_for(scoring, espn_ids=[3112335, 4066648, 5550001])

    expected = _expected_values(BASELINE, NINE_CAT)
    assert out[3112335] == ValueResult(expected[101], "baseline")
    assert out[4066648] == ValueResult(expected[102], "baseline")
    assert out[5550001] == ValueResult(None, None)                       # rookie: no baseline either
    assert all(r.is_baseline for r in out.values() if r.value is not None)
    assert pools.calls["baseline"] == 1

    pools.state["baseline"] = []
    assert PlayerValueService.avg_points_for(scoring, espn_ids=[3112335]) == {3112335: ValueResult(None, None)}


@pytest.mark.unit
def test_category_value_by_name_normalizes_and_disambiguates_by_team(pools):
    scoring = resolve_scoring(_league("categories", NINE_CAT_JSON))
    names = [("Nikola Jokić", "DEN"), ("Twin Name", "MIA"), ("Twin Name", "BOS"), ("twin name", "XXX"), ("Nobody", "SAS")]

    out = PlayerValueService.avg_points_for(scoring, names=names)

    expected = _expected_values(ROLLING, NINE_CAT)
    assert out["nikola jokic"] == ValueResult(expected[101], "rolling")  # accent-stripped key
    assert out["twin name"].value in (expected[103], expected[104])       # same key, last write wins...
    assert out["nobody"] == ValueResult(None, None)
    # ...but each lookup resolved to the player on the expected team
    by_team = {t: PlayerValueService.category_value_by_name([("Twin Name", t)], NINE_CAT)["twin name"].value
               for t in ("MIA", "BOS", "XXX")}
    assert by_team["MIA"] == expected[104] and by_team["BOS"] == expected[103]
    assert by_team["XXX"] in (expected[103], expected[104])              # unknown team: first candidate


@pytest.mark.unit
def test_arbitrary_day_counts_snap_to_a_stored_window_and_recent_means_the_shortest(pools):
    scoring = resolve_scoring(_league("categories", NINE_CAT_JSON))
    for days, window in ((3, 7), (7, 7), (10, 7), (14, 14), (22, 14), (30, 30)):
        PlayerValueService.avg_points_for(scoring, espn_ids=[3112335], days=days)
        assert pools.calls["rolling"][-1] == window, days
    PlayerValueService.avg_points_for(scoring, espn_ids=[3112335], days=14, recent=True)
    assert pools.calls["rolling"][-1] == 7


# ---- points leagues -----------------------------------------------------------------


@pytest.mark.unit
def test_points_league_scores_fantasy_points_under_the_league_weights(pools, monkeypatch):
    seen = {}

    def fake_rolling(ids, days, weights):
        seen["rolling"] = (list(ids), days, dict(weights))
        return {1: 30.0, 2: None}

    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id", staticmethod(fake_rolling))
    line = StatLine.from_dict({"pts": 20, "reb": 5})
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_espn_id", staticmethod(lambda ids, season=None: {2: line}))

    scoring = resolve_scoring(_league(weights={"pts": 2.0, "reb": 1.0}))
    assert PlayerValueService.value_kind_for(scoring) == "fpts"
    out = PlayerValueService.avg_points_for(scoring, espn_ids=[1, 2], days=14)

    assert seen["rolling"] == ([1, 2], 14, {"pts": 2.0, "reb": 1.0})
    assert out[1] == ValueResult(30.0, "rolling")
    assert out[2] == ValueResult(45.0, "baseline")                       # 20*2 + 5*1 under the league weights
    assert pools.calls["rolling"] == [] and pools.calls["baseline"] == 0  # no category pool was loaded


@pytest.mark.unit
def test_points_league_recent_and_by_name_paths(monkeypatch):
    monkeypatch.setattr(PlayerValueService, "recent_weighted_avg_by_espn_id",
                        staticmethod(lambda ids, days, hl, weights: {1: 22.5}))
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_espn_id", staticmethod(lambda ids, season=None: {}))
    scoring = resolve_scoring(None)
    assert scoring.point_weights == DEFAULT_POINT_WEIGHTS
    assert PlayerValueService.avg_points_for(scoring, espn_ids=[1], recent=True) == {1: ValueResult(22.5, "recent")}

    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_name",
                        staticmethod(lambda players, days, weights: {"nikola jokic": 55.5}))
    out = PlayerValueService.avg_points_for(scoring, names=[("Nikola Jokić", "DEN")])
    assert out == {"nikola jokic": ValueResult(55.5, "rolling")}

    monkeypatch.setattr(PlayerValueService, "recent_weighted_avg_by_name",
                        staticmethod(lambda players, days, hl, weights: {"nikola jokic": 58.0}))
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_name", staticmethod(lambda players, season=None: {}))
    out = PlayerValueService.avg_points_for(scoring, names=[("Nikola Jokić", "DEN")], recent=True)
    assert out == {"nikola jokic": ValueResult(58.0, "recent")}


@pytest.mark.unit
def test_points_preview_on_a_category_league_scores_points(monkeypatch):
    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id", staticmethod(lambda ids, days, weights: {1: 12.0}))
    scoring = resolve_scoring(_league("categories", NINE_CAT_JSON), preview="points")
    assert PlayerValueService.value_kind_for(scoring) == "fpts"
    assert PlayerValueService.avg_points_for(scoring, espn_ids=[1]) == {1: ValueResult(12.0, "rolling")}


@pytest.mark.unit
def test_empty_inputs_never_touch_the_pool(pools):
    cat = resolve_scoring(_league("categories", NINE_CAT_JSON))
    assert PlayerValueService.avg_points_for(cat, espn_ids=[]) == {}
    assert PlayerValueService.avg_points_for(cat, names=[]) == {}
    assert PlayerValueService.avg_points_for(cat) == {}
    assert PlayerValueService.avg_points_for(resolve_scoring(None), espn_ids=[]) == {}
    assert pools.calls["rolling"] == [] and pools.calls["baseline"] == 0


@pytest.mark.unit
def test_scoring_for_resolves_a_team_first_then_league_info(monkeypatch):
    from services.scoring import resolver

    monkeypatch.setattr(resolver, "resolve_scoring_for_team", lambda team_id: ResolvedScoring("categories", None, True, {}, None))
    monkeypatch.setattr(resolver, "resolve_scoring_for_league_info", lambda li: ResolvedScoring("points", None, True, {"pts": 3.0}))
    li = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027)

    assert PlayerValueService.scoring_for(li, team_id=7).format == "categories"
    assert PlayerValueService.scoring_for(li).point_weights == {"pts": 3.0}
    assert PlayerValueService.scoring_for(None).point_weights == DEFAULT_POINT_WEIGHTS
