"""ESPN settings / matchup parsers against captured fixtures plus a synthetic category league."""

import json
from pathlib import Path

import pytest

from services.scoring import DEFAULT_POINT_WEIGHTS
from services.scoring.providers.espn_settings import (
    parse_espn_category_score,
    parse_espn_settings,
    statline_from_espn_stats,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.unit
def test_points_league_sample_yields_exact_default_weights():
    s = parse_espn_settings(_load("espn_settings_h2h_points.json"))
    assert s.provider == "espn" and s.scoring_type == "points" and s.category_win_mode is None
    assert s.point_weights == DEFAULT_POINT_WEIGHTS
    assert s.categories == [] and s.unsupported == [] and s.warnings == []
    assert s.roster_slots == {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1}
    periods = s.matchup_periods["periods"]
    assert periods and all(isinstance(v, list) and all(isinstance(x, int) for x in v) for v in periods.values())
    assert isinstance(s.matchup_periods["period_count"], int) and s.matchup_periods["period_count"] >= 18
    assert s.matchup_periods["playoff_period_length"] in (1, 2)
    assert s.season == 2026 and s.provider_league_id.isdigit()


def _category_settings(scoring_type: str = "H2H_CATEGORY") -> dict:
    items = [{"statId": sid, "points": 0.0, "isReverseItem": sid == 11}
             for sid in (19, 20, 17, 0, 6, 3, 2, 1, 11)]
    return {"id": 555, "seasonId": 2027, "settings": {
        "name": "Cat Test", "scoringSettings": {"scoringType": scoring_type, "scoringItems": items},
        "scheduleSettings": {"matchupPeriods": {"1": [1]}, "matchupPeriodCount": 19},
        "rosterSettings": {"lineupSlotCounts": {"0": 1, "11": 3, "12": 3, "13": 1}},
    }}


@pytest.mark.unit
def test_category_league_items_become_ordered_categories_with_turnover_inverted():
    s = parse_espn_settings(_category_settings())
    assert s.scoring_type == "categories" and s.category_win_mode == "each_category"
    assert [c.key for c in s.categories] == ["fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"]
    tov = next(c for c in s.categories if c.key == "tov")
    assert tov.higher_is_better is False          # isReverseItem: true -> lower is better
    assert next(c for c in s.categories if c.key == "pts").higher_is_better is True
    assert s.point_weights == {}
    assert next(c for c in s.categories if c.key == "fg_pct").is_rate


@pytest.mark.unit
def test_most_categories_and_unknown_ids():
    payload = _category_settings("H2H_MOST_CATEGORIES")
    payload["settings"]["scoringSettings"]["scoringItems"].append({"statId": 99, "points": 1})
    s = parse_espn_settings(payload)
    assert s.category_win_mode == "most_categories"
    assert s.unsupported == ["espn:99:?"]


@pytest.mark.unit
def test_unknown_scoring_type_falls_back_to_points_with_warning():
    payload = _category_settings("SOMETHING_NEW")
    s = parse_espn_settings(payload)
    assert s.scoring_type == "points" and any("SOMETHING_NEW" in w for w in s.warnings)


@pytest.mark.unit
def test_cumulative_score_parses_from_captured_matchup():
    m = _load("espn_matchup_h2h_points.json")
    home = m["schedule"][0]["home"]
    score = parse_espn_category_score(home)
    assert score is not None
    assert score.totals["pts"] > 0 and score.totals["reb"] > 0 and score.totals["tov"] > 0
    assert score.raw is not None and score.raw["fgm"] <= score.raw["fga"]
    assert (score.wins, score.losses, score.ties) == (0, 0, 0)     # points league: no category results
    assert parse_espn_category_score({"totalPoints": 1.0}) is None


@pytest.mark.unit
def test_statline_from_espn_stats_maps_ids():
    line = statline_from_espn_stats({"0": 25.5, "6": 8, "11": 2, "13": 9, "14": 18, "99": 1})
    assert (line.pts, line.reb, line.tov, line.fgm, line.fga) == (25.5, 8, 2, 9, 18)
    assert line.get("fg_pct") == 0.5
