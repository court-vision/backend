"""
Unit: run_query_stats around a scripted reader (docs/QUERY_STATS_PLAN.md step 2).

Coverage decides the default season and refuses seasons outside it; the rows
come back plain (floats and ISO dates) with `series` for charts, `labels` for
the derived columns, and a note for what the spec asked for and did not get.
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from services.ai.stats import query
from services.ai.stats.compiler import ROW_CAP, StatsSpec
from services.ai.stats.query import SpecError, run_query_stats

JOKIC, WEMBY = 203999, 1641705
TODAY = date(2026, 1, 15)


class FakeReader:
    """Answers the coverage query from `span` and every other statement from `rows`."""

    def __init__(self, rows=(), span=("2012-13", "2025-26")):
        self.rows = list(rows)
        self.span = span
        self.calls: list[tuple[str, tuple]] = []

    async def query(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if sql.startswith("SELECT MIN(season)"):
            return [{"first": self.span[0], "last": self.span[1]}]
        return [dict(r) for r in self.rows]


def game(player_id, name, game_no, season="2025-26", season_type="regular", **stats):
    return {"player_id": player_id, "name": name, "season": season, "season_type": season_type, "game_no": game_no,
            "date": date(2025, 10, 20) + timedelta(days=game_no), "opponent": "SAS", "home": True, **stats}


def run(spec_raw, reader):
    return asyncio.run(run_query_stats(StatsSpec.model_validate(spec_raw), reader=reader, today=TODAY))


@pytest.fixture(autouse=True)
def fresh_coverage():
    query.reset_coverage_cache()
    yield
    query.reset_coverage_cache()


@pytest.mark.unit
class TestSeasons:
    def test_game_and_total_default_to_the_latest_season_with_games(self):
        reader = FakeReader([game(JOKIC, "Nikola Jokic", 1, pts=30)])
        out = run({"players": [JOKIC], "grain": "game", "metrics": ["pts"]}, reader)
        assert out["seasons"] == ["2025-26"]
        assert reader.calls[-1][1][2] == ["2025-26"]
        out = run({"players": [JOKIC], "grain": "total", "metrics": ["pts"]}, reader)
        assert out["seasons"] == ["2025-26"]

    def test_season_grain_defaults_to_every_season(self):
        reader = FakeReader([{"player_id": JOKIC, "name": "Nikola Jokic", "season": "2024-25", "gp": 70, "pts": Decimal("28.6")}])
        out = run({"players": [JOKIC], "grain": "season", "metrics": ["pts"]}, reader)
        assert out["seasons"] == "all"
        assert "season = ANY" not in reader.calls[-1][0]

    def test_a_season_outside_the_history_is_refused_with_the_range(self):
        with pytest.raises(SpecError, match="covers 2012-13 to 2025-26; 2011-12 is outside it"):
            run({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "seasons": ["2011-12", "2024-25"]}, FakeReader())

    def test_no_history_at_all_is_a_readable_error(self):
        with pytest.raises(SpecError, match="no game history"):
            run({"players": [JOKIC], "grain": "game", "metrics": ["pts"]}, FakeReader(span=(None, None)))

    def test_coverage_is_read_once_an_hour(self):
        reader = FakeReader([game(JOKIC, "Nikola Jokic", 1, pts=30)])
        run({"players": [JOKIC], "grain": "game", "metrics": ["pts"]}, reader)
        run({"players": [JOKIC], "grain": "game", "metrics": ["pts"]}, reader)
        assert sum(sql.startswith("SELECT MIN(season)") for sql, _ in reader.calls) == 1


@pytest.mark.unit
class TestRows:
    def test_values_come_back_plain_with_series_and_labels(self):
        reader = FakeReader([
            game(JOKIC, "Nikola Jokic", 1, fpts=60, fpts_rm2=None),
            game(JOKIC, "Nikola Jokic", 2, fpts=50, fpts_rm2=Decimal("55.0")),
            game(WEMBY, "Victor Wembanyama", 1, fpts=40, fpts_rm2=None),
        ])
        out = run({"players": [JOKIC, WEMBY], "grain": "game", "metrics": ["fpts"], "transform": {"rolling_mean": 2}}, reader)
        assert out["grain"] == "game" and out["season_type"] == "regular" and "agg" not in out
        assert out["labels"] == {"fpts_rm2": "CV fantasy points, 2-game rolling mean"}
        assert out["rows"][1] == {"player_id": JOKIC, "name": "Nikola Jokic", "season": "2025-26", "season_type": "regular",
                                  "game_no": 2, "date": "2025-10-22", "opponent": "SAS", "home": True, "fpts": 50,
                                  "fpts_rm2": 55.0, "series": "Nikola Jokic"}
        assert [r["series"] for r in out["rows"]] == ["Nikola Jokic", "Nikola Jokic", "Victor Wembanyama"]
        assert out["total_rows"] == 3
        assert out["note"] == "fpts is Court Vision's fantasy points formula, not the user's league scoring"

    def test_series_stay_unique_across_seasons_and_season_types(self):
        reader = FakeReader([
            game(JOKIC, "Nikola Jokic", 1, season="2024-25", pts=1),
            game(JOKIC, "Nikola Jokic", 1, season="2025-26", pts=2),
            game(JOKIC, "Nikola Jokic", 1, season="2025-26", season_type="play_in", pts=3),
        ])
        out = run({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "seasons": ["2024-25", "2025-26"],
                   "season_type": "all"}, reader)
        assert [r["series"] for r in out["rows"]] == [
            "Nikola Jokic · 2024-25 · regular", "Nikola Jokic · 2025-26 · regular", "Nikola Jokic · 2025-26 · play-in"]

    def test_window_agg_and_min_games_are_echoed(self):
        reader = FakeReader([{"player_id": JOKIC, "name": "Nikola Jokic", "gp": 5, "first_date": date(2025, 11, 1),
                              "last_date": date(2025, 11, 9), "pts": Decimal("27.4")}])
        out = run({"grain": "total", "metrics": ["pts"], "window": {"from": "2025-11-01", "to": "2025-11-10"},
                   "rank": {"by": "pts"}, "min_games": 5}, reader)
        assert out["agg"] == "per_game" and out["min_games"] == 5
        assert out["window"] == {"from": "2025-11-01", "to": "2025-11-10"}
        assert out["rows"][0]["first_date"] == "2025-11-01" and out["rows"][0]["pts"] == 27.4
        assert "series" not in out["rows"][0]

    def test_more_than_the_cap_is_refused_with_how_to_narrow(self):
        reader = FakeReader([game(JOKIC, "Nikola Jokic", i, pts=i) for i in range(1, ROW_CAP + 2)])
        with pytest.raises(SpecError, match=f"Over {ROW_CAP} rows. Narrow it"):
            run({"players": [JOKIC], "grain": "game", "metrics": ["pts"]}, reader)


@pytest.mark.unit
class TestNotes:
    def test_a_player_with_no_games_is_named(self):
        reader = FakeReader([game(JOKIC, "Nikola Jokic", 1, pts=30)])
        out = run({"players": [JOKIC, WEMBY], "grain": "game", "metrics": ["pts"], "seasons": ["2025-26"],
                   "season_type": "playoffs"}, reader)
        assert out["note"] == f"No playoffs games in 2025-26 for player_id {WEMBY}"

    def test_a_season_a_player_skipped_is_named(self):
        reader = FakeReader([
            {"player_id": JOKIC, "name": "Nikola Jokic", "season": "2024-25", "gp": 70, "pts": 28},
            {"player_id": JOKIC, "name": "Nikola Jokic", "season": "2025-26", "gp": 10, "pts": 30},
            {"player_id": WEMBY, "name": "Victor Wembanyama", "season": "2025-26", "gp": 7, "pts": 24},
        ])
        out = run({"players": [JOKIC, WEMBY], "grain": "season", "metrics": ["pts"], "seasons": ["2024-25", "2025-26"]}, reader)
        assert out["note"] == f"player_id {WEMBY} has no games in 2024-25"

    def test_no_rows_for_leaders_says_so(self):
        out = run({"grain": "total", "metrics": ["pts"], "agg": "count_games",
                   "where": {"stats": [{"metric": "pts", "op": ">=", "value": 70}]}, "rank": {"by": "gp"}}, FakeReader())
        assert out["rows"] == [] and out["total_rows"] == 0
        assert out["note"] == "No rows matched"
        assert out["min_games"] == 1

    def test_all_season_types_read_as_games(self):
        out = run({"players": [JOKIC], "grain": "total", "metrics": ["pts"], "season_type": "all"}, FakeReader())
        assert out["note"] == f"No games in 2025-26 for player_id {JOKIC}"
