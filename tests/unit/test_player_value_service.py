"""Composed player values: current window first, last season's per-game baseline as the fallback."""

import pytest

from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.models import StatLine
from services.scoring.points import PointsScoring
from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

BASE_LINE = StatLine.from_dict({"pts": 20, "reb": 5, "ast": 5, "stl": 1, "blk": 1, "tov": 2, "fg3m": 2,
                                "fgm": 8, "fga": 16, "ftm": 2, "fta": 3})


@pytest.mark.unit
def test_value_by_espn_id_uses_rolling_then_baseline(monkeypatch):
    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id",
                        staticmethod(lambda ids, days, weights: {1: 30.0, 2: None, 3: None}))
    seen = {}

    def fake_baseline(ids, season=None):
        seen["ids"] = ids
        return {2: BASE_LINE, 3: None}

    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_espn_id", staticmethod(fake_baseline))

    out = PlayerValueService.value_by_espn_id([1, 2, 3], weights=None, days=14)
    assert seen["ids"] == [2, 3]                                   # only the players the window couldn't value
    assert out[1] == ValueResult(30.0, "rolling")
    assert out[2].source == "baseline" and out[2].value == round(PointsScoring(DEFAULT_POINT_WEIGHTS).score(BASE_LINE), 1)
    assert out[3] == ValueResult(None, None)


@pytest.mark.unit
def test_baseline_is_scored_with_the_league_weights(monkeypatch):
    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id", staticmethod(lambda ids, days, weights: {7: None}))
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_espn_id", staticmethod(lambda ids, season=None: {7: BASE_LINE}))
    weights = {"pts": 2.0}
    out = PlayerValueService.value_by_espn_id([7], weights=weights)
    assert out[7].value == round(PointsScoring(weights).score(BASE_LINE), 1) == 40.0


@pytest.mark.unit
def test_recent_and_by_name_variants(monkeypatch):
    monkeypatch.setattr(PlayerValueService, "recent_weighted_avg_by_espn_id",
                        staticmethod(lambda ids, days, hl, weights: {1: 22.5, 2: None}))
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_espn_id", staticmethod(lambda ids, season=None: {2: BASE_LINE}))
    recent = PlayerValueService.recent_value_by_espn_id([1, 2])
    assert recent[1] == ValueResult(22.5, "recent") and recent[2].is_baseline

    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_name",
                        staticmethod(lambda players, days, weights: {"nikola jokic": None, "role player": 12.0}))
    monkeypatch.setattr(PlayerValueService, "baseline_lines_by_name",
                        staticmethod(lambda players, season=None: {"nikola jokic": BASE_LINE}))
    by_name = PlayerValueService.value_by_name([("Nikola Jokić", "DEN"), ("Role Player", "LAL")])
    assert by_name["nikola jokic"].is_baseline and by_name["role player"] == ValueResult(12.0, "rolling")


@pytest.mark.unit
def test_empty_inputs():
    assert PlayerValueService.value_by_espn_id([]) == {}
    assert PlayerValueService.value_by_name([]) == {}
    assert PlayerValueService.recent_value_by_espn_id([]) == {}
