"""Yahoo settings / matchup parsers on synthetic fixtures shaped per the Yahoo Fantasy API docs.

The connected Yahoo app currently returns 403 for every Fantasy endpoint, so real
fixtures could not be captured; these tests pin the documented shape only.
"""

import pytest

from services.scoring import CategoryDef, DEFAULT_CATEGORIES
from services.scoring.providers.yahoo_settings import (
    parse_yahoo_matchup_categories,
    parse_yahoo_settings,
    statline_from_yahoo_stats,
)


def _settings_payload(scoring_type: str, with_modifiers: bool = False) -> dict:
    cats = [
        (5, "FGA", "1", "1"), (6, "FGM", "1", "1"), (7, "FG%", "1", "0"), (8, "FTA", "1", "1"), (9, "FTM", "1", "1"),
        (10, "FT%", "1", "0"), (12, "3PTM", "1", "0"), (15, "PTS", "1", "0"), (18, "REB", "1", "0"),
        (19, "AST", "1", "0"), (20, "ST", "1", "0"), (21, "BLK", "1", "0"), (22, "TO", "0", "0"),
    ]
    stats = [{"stat": {"stat_id": sid, "enabled": "1", "display_name": name, "sort_order": order,
                       "is_only_display_stat": display}} for sid, name, order, display in cats]
    settings = {
        "roster_positions": [{"roster_position": {"position": p, "count": c}} for p, c in
                             (("PG", 1), ("SG", 1), ("G", 1), ("SF", 1), ("PF", 1), ("F", 1), ("C", 2),
                              ("Util", 2), ("BN", 3), ("IL", 2))],
        "stat_categories": {"stats": stats},
        "playoff_start_week": "19",
    }
    if with_modifiers:
        settings["stat_modifiers"] = {"stats": [{"stat": {"stat_id": sid, "value": v}} for sid, v in
                                                ((15, "1"), (18, "1.2"), (19, "1.5"), (20, "3"), (21, "3"),
                                                 (22, "-1"), (12, "0.5"), (7, "5"))]}
    return {"fantasy_content": {"league": [
        {"league_key": "466.l.368978", "league_id": "368978", "name": "Test League", "season": "2025",
         "scoring_type": scoring_type, "start_week": "1", "end_week": "22", "current_week": "22"},
        {"settings": [settings]},
    ]}}


@pytest.mark.unit
def test_head_to_head_categories_league():
    s = parse_yahoo_settings(_settings_payload("head"))
    assert s.provider == "yahoo" and s.provider_league_id == "466.l.368978" and s.season == 2025
    assert s.scoring_type == "categories" and s.category_win_mode == "each_category"
    assert [c.key for c in s.categories] == ["fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"]
    assert next(c for c in s.categories if c.key == "tov").higher_is_better is False   # sort_order "0"
    assert s.matchup_periods["period_count"] == 22 and s.matchup_periods["playoff_start_week"] == 19
    assert s.roster_slots["IR"] == 2 and s.roster_slots["UT"] == 2 and s.roster_slots["BE"] == 3
    assert s.point_weights == {} and s.unsupported == []


@pytest.mark.unit
def test_points_league_modifiers_become_weights_and_rate_stats_are_rejected():
    s = parse_yahoo_settings(_settings_payload("headpoint", with_modifiers=True))
    assert s.scoring_type == "points" and s.category_win_mode is None
    assert s.point_weights == {"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "tov": -1.0, "fg3m": 0.5}
    assert any("fg_pct" in w for w in s.warnings)


@pytest.mark.unit
def test_most_categories_and_roto_and_unknown():
    assert parse_yahoo_settings(_settings_payload("headone")).category_win_mode == "most_categories"
    assert parse_yahoo_settings(_settings_payload("roto")).scoring_type == "roto"
    s = parse_yahoo_settings(_settings_payload("weird"))
    assert s.scoring_type == "points" and s.warnings


@pytest.mark.unit
def test_statline_splits_composite_display_stats():
    line = statline_from_yahoo_stats([
        {"stat": {"stat_id": "9004003", "value": "245/510"}},
        {"stat": {"stat_id": "15", "value": "612"}},
        {"stat": {"stat_id": "7", "value": ".480"}},   # rate value is ignored; derived from makes/attempts
        {"stat": {"stat_id": "22", "value": "-"}},
    ])
    assert (line.fgm, line.fga, line.pts, line.tov) == (245, 510, 612, 0)
    assert line.get("fg_pct") == pytest.approx(245 / 510)


@pytest.mark.unit
def test_matchup_categories_with_stat_winners():
    def team(key, pts, fgm, fga):
        return {"team": [[{"team_key": key}, {"name": key}],
                         {"team_stats": {"coverage_type": "week", "stats": [
                             {"stat": {"stat_id": "15", "value": str(pts)}},
                             {"stat": {"stat_id": "9004003", "value": f"{fgm}/{fga}"}},
                             {"stat": {"stat_id": "22", "value": "30"}},
                         ]}}]}
    matchup = {
        "week": "5", "status": "postevent",
        "stat_winners": [
            {"stat_winner": {"stat_id": "15", "winner_team_key": "466.l.1.t.1"}},
            {"stat_winner": {"stat_id": "7", "winner_team_key": "466.l.1.t.2"}},
            {"stat_winner": {"stat_id": "22", "is_tied": 1}},
        ],
        "0": {"teams": {"0": team("466.l.1.t.1", 500, 200, 400), "1": team("466.l.1.t.2", 450, 190, 360), "count": 2}},
    }
    cats = [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]
    you, opp = parse_yahoo_matchup_categories(matchup, "466.l.1.t.1", cats)
    assert you.totals["pts"] == 500 and opp.totals["pts"] == 450
    assert you.totals["fg_pct"] == pytest.approx(0.5, abs=1e-4) and opp.raw["fga"] == 360
    assert (you.wins, you.losses, you.ties) == (1, 1, 1) and (opp.wins, opp.losses) == (1, 1)
    assert parse_yahoo_matchup_categories({"0": {"teams": {}}}, "466.l.1.t.1", cats) is None
