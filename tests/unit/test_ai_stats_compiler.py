"""
Unit: the query_stats spec and its compiler (docs/QUERY_STATS_PLAN.md step 2).

The spec refuses what the views cannot serve, with the fix in the message;
the compiler binds every value the model supplies and takes every identifier
from the catalog; the shapes (game, total, season from the view or from game
rows, leaders) come out as the plan describes them.
"""

import re
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from services.ai.stats import compiler
from services.ai.stats.catalog import GAMES_VIEW, METRIC_NAMES, SEASONS_VIEW
from services.ai.stats.compiler import ROW_CAP, StatsSpec, compile_spec

TODAY = date(2026, 1, 15)
JOKIC, WEMBY = 203999, 1641705


def spec(**raw) -> StatsSpec:
    return StatsSpec.model_validate(raw)


def compiled(seasons=("2025-26",), **raw) -> compiler.Compiled:
    return compile_spec(spec(**raw), seasons=list(seasons) if seasons else None, today=TODAY)


def _one_line(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


@pytest.mark.unit
class TestSpecValidation:
    @pytest.mark.parametrize("raw, message", [
        ({"grain": "game", "metrics": ["pts"]}, "grain game needs players"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "agg": "total"}, "agg applies to grain total or season"),
        ({"players": [JOKIC], "grain": "total", "metrics": ["pts"], "transform": {"rolling_mean": 5}},
         "applies to grain game only"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["gp"]}, "gp: a season figure"),
        ({"players": [JOKIC], "grain": "total", "metrics": ["dd2"]}, "dd2 is only on whole seasons"),
        ({"players": [JOKIC], "grain": "season", "metrics": ["dd2"], "where": {"home": True}},
         "only on whole regular seasons"),
        ({"players": [JOKIC], "grain": "season", "metrics": ["dd2"], "season_type": "playoffs"},
         "only on whole regular seasons"),
        ({"players": [JOKIC], "grain": "season", "metrics": ["td3"], "agg": "max"}, "max applies to game stats"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "agg": "count_games"},
         "agg applies to grain total or season"),
        ({"grain": "total", "metrics": ["pts"]}, "give rank"),
        ({"players": [JOKIC], "grain": "total", "metrics": ["pts"], "min_games": 5}, "min_games is for leaders"),
        ({"grain": "total", "metrics": ["pts"], "rank": {"by": "reb"}}, "rank.by must be one of the metrics"),
        ({"grain": "total", "metrics": ["pts"], "agg": "count_games", "rank": {"by": "pts"}}, "rank by 'gp'"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["ppg"]}, "unknown metric 'ppg'"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts", "pts"]}, "listed twice"),
        ({"players": [JOKIC, JOKIC], "grain": "game", "metrics": ["pts"]}, "listed twice"),
        ({"players": [0], "grain": "game", "metrics": ["pts"]}, "positive NBA player_ids"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "seasons": ["2025"]}, "should look like '2025-26'"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "window": {"last_games": 5, "last_days": 7}},
         "one of last_games, last_days"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "window": {}}, "one of last_games, last_days"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"],
          "window": {"from": "2025-12-01", "to": "2025-11-01"}}, "from is after to"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "transform": {"rolling_mean": 5, "cumulative_mean": True}},
         "one of them"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "transform": {}}, "one of them"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"],
          "where": {"stats": [{"metric": "gp", "op": ">=", "value": 1}]}}, "'gp' is not a game stat"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "where": {"opponent": "boston"}}, "pattern"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts"], "limit": 5}, "Extra inputs"),
        ({"players": list(range(1, 10)), "grain": "game", "metrics": ["pts"]}, "at most 8 items"),
        ({"players": [JOKIC], "grain": "game", "metrics": ["pts", "reb", "ast", "stl", "blk", "tov", "min"]},
         "at most 6 items"),
    ])
    def test_a_spec_the_views_cannot_serve_is_refused_with_the_fix(self, raw, message):
        with pytest.raises(ValidationError, match=re.escape(message)):
            StatsSpec.model_validate(raw)

    def test_the_plan_s_shapes_validate(self):
        spec(players=[WEMBY, JOKIC], grain="game", metrics=["fpts"], seasons=["2024-25"], transform={"rolling_mean": 5})
        spec(players=[JOKIC], grain="total", metrics=["pts", "fg_pct"], window={"last_games": 10})
        spec(grain="total", metrics=["pts"], agg="count_games",
             where={"stats": [{"metric": "pts", "op": ">=", "value": 40}]}, rank={"by": "gp", "top": 5})
        spec(grain="season", metrics=["fpts", "gp"], seasons=["2024-25"], rank={"by": "fpts"}, min_games=50)
        spec(players=[JOKIC], grain="season", metrics=["pts", "gp", "dd2", "td3", "usg_pct", "fg_pct"])
        spec(players=[JOKIC], grain="total", metrics=["gp", "pts"], window={"from": "2025-01-01", "to": "2025-02-01"})
        spec(players=[JOKIC], grain="game", metrics=["pts"], rank={"by": "pts", "top": 5})

    def test_rank_may_name_a_transform_column_at_game_grain(self):
        s = spec(players=[JOKIC], grain="game", metrics=["fpts"], transform={"rolling_mean": 5},
                 rank={"by": "fpts_rm5", "top": 3})
        assert s.rank.by == "fpts_rm5"
        with pytest.raises(ValidationError, match="rank.by must be one of the metrics"):
            spec(players=[JOKIC], grain="game", metrics=["fpts"], rank={"by": "fpts_rm5"})

    def test_the_window_reads_from_and_to_by_their_json_names(self):
        s = spec(players=[JOKIC], grain="total", metrics=["pts"], window={"from": "2025-01-01", "to": "2025-02-01"})
        assert (s.window.from_date, s.window.to_date) == (date(2025, 1, 1), date(2025, 2, 1))
        assert s.window.model_dump(by_alias=True, exclude_none=True) == {"from": date(2025, 1, 1), "to": date(2025, 2, 1)}


@pytest.mark.unit
class TestWhatTheCompilerReads:
    def test_season_grain_uses_the_season_view_only_when_nothing_narrows_the_season(self):
        base = dict(players=[JOKIC], grain="season", metrics=["pts"])
        assert spec(**base)._from_game_rows() is False
        assert spec(**{**base, "metrics": ["pts", "gp", "dd2", "fg_pct"]})._from_game_rows() is False
        for narrowing in (dict(where={"home": True}), dict(window={"last_games": 10}), dict(season_type="playoffs"),
                          dict(agg="max"), dict(agg="count_games"), dict(metrics=["plus_minus"])):
            assert spec(**{**base, **narrowing})._from_game_rows() is True, narrowing
        assert spec(players=[JOKIC], grain="total", metrics=["pts"])._from_game_rows() is True

    def test_min_games_defaults(self):
        leaders = dict(grain="total", metrics=["pts"], rank={"by": "pts"})
        assert spec(**leaders).effective_min_games() == 10
        assert spec(**leaders, window={"last_games": 5}).effective_min_games() == 5
        assert spec(**leaders, window={"last_games": 20}).effective_min_games() == 10
        assert spec(**leaders, window={"last_days": 7}).effective_min_games() == 10
        assert spec(**leaders, min_games=41).effective_min_games() == 41
        assert spec(grain="total", metrics=["pts"], agg="count_games", rank={"by": "gp"}).effective_min_games() == 1

    def test_the_transform_names_its_column(self):
        assert spec(players=[JOKIC], grain="game", metrics=["pts"], transform={"rolling_mean": 5}).transform.suffix == "_rm5"
        assert spec(players=[JOKIC], grain="game", metrics=["pts"], transform={"cumulative_mean": True}).transform.suffix == "_cum"

    def test_game_metrics_are_the_catalog_s_game_columns(self):
        assert set(compiler.GAME_METRICS) == set(METRIC_NAMES) - {"gp", "dd2", "td3", "usg_pct"}
        assert set(compiler.SEASON_ONLY_METRICS) == {"gp", "dd2", "td3", "usg_pct"}


@pytest.mark.unit
class TestCompiledGameRows:
    def test_the_owner_s_question(self):
        c = compiled(seasons=["2024-25"], players=[WEMBY, JOKIC], grain="game", metrics=["fpts"],
                     transform={"rolling_mean": 5})
        sql = _one_line(c.sql)
        assert c.source == "games" and GAMES_VIEW in sql
        assert sql.startswith("SELECT player_id, name, season, season_type, game_no, date, opponent, home, fpts, fpts_rm5 FROM")
        frame = ("OVER (PARTITION BY player_id, season, season_type ORDER BY date, game_no "
                 "ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)")
        assert f"CASE WHEN COUNT(*) {frame} >= 5 THEN ROUND((AVG(fpts) {frame})::numeric, 1) END AS fpts_rm5" in sql
        assert "WHERE min >= %s AND player_id = ANY(%s) AND season = ANY(%s) AND season_type = %s" in sql
        assert sql.endswith("ORDER BY player_id, season, season_type, date LIMIT %s")
        assert c.params == (1, [WEMBY, JOKIC], ["2024-25"], "regular", ROW_CAP + 1)
        # Only the derived column needs a label; fpts keeps the display tools' header
        assert c.labels == {"fpts_rm5": "CV fantasy points, 5-game rolling mean"}

    def test_a_cumulative_mean_runs_from_the_season_s_start_and_a_rate_is_makes_over_attempts(self):
        c = compiled(players=[JOKIC], grain="game", metrics=["fg_pct"], transform={"cumulative_mean": True})
        sql = _one_line(c.sql)
        frame = ("OVER (PARTITION BY player_id, season, season_type ORDER BY date, game_no "
                 "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)")
        assert f"ROUND((SUM(fgm) {frame}::numeric / NULLIF(SUM(fga) {frame}, 0))::numeric, 3) AS fg_pct_cum" in sql
        assert "CASE WHEN" not in sql
        assert "ROUND((fgm::numeric / NULLIF(fga, 0))::numeric, 3) AS fg_pct" in sql
        assert c.labels == {"fg_pct_cum": "field goal %, season-to-date mean"}

    def test_filters_bind_every_value(self):
        c = compiled(players=[JOKIC], grain="game", metrics=["pts"], season_type="all",
                     window={"last_days": 30},
                     where={"stats": [{"metric": "pts", "op": ">=", "value": 40}, {"metric": "fg_pct", "op": "<", "value": 0.4}],
                            "opponent": "BOS", "home": False, "min_minutes": 20})
        sql = _one_line(c.sql)
        assert ("WHERE min >= %s AND player_id = ANY(%s) AND season = ANY(%s) AND date >= %s AND pts >= %s "
                "AND fgm::numeric / NULLIF(fga, 0) < %s AND opponent = %s AND home = %s") in sql
        assert "season_type = " not in sql
        assert c.params == (20, [JOKIC], ["2025-26"], TODAY - timedelta(days=30), 40.0, 0.4, "BOS", False, ROW_CAP + 1)
        for literal in ("203999", "2025-26", "BOS", "40"):
            assert literal not in sql

    def test_last_games_keeps_each_player_s_latest_rows_after_the_transform(self):
        c = compiled(players=[JOKIC, WEMBY], grain="game", metrics=["pts"], transform={"rolling_mean": 3},
                     window={"last_games": 10})
        sql = _one_line(c.sql)
        assert "ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date DESC, game_no DESC) AS rn" in sql
        # The mean is computed over the whole season, inside the selection ROW_NUMBER cuts
        assert sql.index("ROW_NUMBER()") < sql.index("AS pts_rm3") < sql.index("WHERE rn <= %s")
        assert "WHERE rn <= %s" in sql
        assert c.params == (1, [JOKIC, WEMBY], ["2025-26"], "regular", 10, ROW_CAP + 1)

    def test_a_date_window_and_the_from_to_order(self):
        c = compiled(players=[JOKIC], grain="game", metrics=["pts"], window={"from": "2025-11-01", "to": "2025-11-30"})
        assert "date >= %s AND date <= %s" in _one_line(c.sql)
        assert c.params[-3:] == (date(2025, 11, 1), date(2025, 11, 30), ROW_CAP + 1)

    def test_ranked_games_order_by_the_stat_and_stop_at_top(self):
        c = compiled(players=[JOKIC], grain="game", metrics=["pts", "fpts"], transform={"rolling_mean": 5},
                     rank={"by": "fpts_rm5", "top": 3, "order": "asc"})
        assert _one_line(c.sql).endswith("ORDER BY fpts_rm5 ASC NULLS LAST, date LIMIT %s")
        assert c.params[-1] == 3


@pytest.mark.unit
class TestCompiledGroups:
    def test_totals_over_a_window_group_the_game_rows(self):
        c = compiled(players=[JOKIC], grain="total", metrics=["pts", "fg_pct", "gp"], window={"last_games": 10})
        sql = _one_line(c.sql)
        assert sql.startswith("SELECT player_id, name, COUNT(*) AS gp, MIN(date) AS first_date, MAX(date) AS last_date, "
                              "ROUND((AVG(pts))::numeric, 1) AS pts, "
                              "ROUND((SUM(fgm)::numeric / NULLIF(SUM(fga), 0))::numeric, 3) AS fg_pct FROM")
        assert "SELECT date, fga, fgm, game_no, name, player_id, pts FROM nba.ask_player_games" in sql  # no gp column read
        assert "ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY date DESC, game_no DESC) AS rn" in sql
        assert sql.endswith("GROUP BY player_id, name ORDER BY player_id, name LIMIT %s")
        assert "HAVING" not in sql
        assert c.params == (1, [JOKIC], ["2025-26"], "regular", 10, ROW_CAP + 1)
        assert c.labels == {"gp": "games played", "pts": "points per game", "fg_pct": "field goal %"}

    @pytest.mark.parametrize("agg, expr, label", [
        ("total", "SUM(pts) AS pts", "points, total"),
        ("max", "MAX(pts) AS pts", "points, best game"),
        ("per_game", "ROUND((AVG(pts))::numeric, 1) AS pts", "points per game"),
    ])
    def test_each_agg(self, agg, expr, label):
        c = compiled(players=[JOKIC], grain="total", metrics=["pts"], agg=agg)
        assert expr in _one_line(c.sql)
        assert c.labels["pts"] == label

    def test_leaders_count_the_games_that_passed_where(self):
        c = compiled(grain="total", metrics=["pts"], agg="count_games",
                     where={"stats": [{"metric": "pts", "op": ">=", "value": 40}]}, rank={"by": "gp", "top": 5})
        sql = _one_line(c.sql)
        assert "WHERE min >= %s AND season = ANY(%s) AND season_type = %s AND pts >= %s" in sql
        assert "player_id = ANY" not in sql
        assert sql.endswith("GROUP BY player_id, name HAVING COUNT(*) >= %s ORDER BY gp DESC NULLS LAST, player_id, name LIMIT %s")
        assert c.params == (1, ["2025-26"], "regular", 40.0, 1, 5)
        assert c.labels == {"gp": "games", "pts": "points per game"}

    def test_leaders_over_a_window_qualify_on_games_played(self):
        c = compiled(grain="total", metrics=["blk"], window={"last_games": 7}, rank={"by": "blk"})
        assert c.params == (1, ["2025-26"], "regular", 7, 7, 10)

    def test_season_grain_from_game_rows_groups_by_season_too(self):
        c = compiled(seasons=None, players=[JOKIC], grain="season", metrics=["pts"], where={"home": True})
        sql = _one_line(c.sql)
        assert c.source == "games"
        assert sql.startswith("SELECT player_id, name, season, COUNT(*) AS gp, ROUND((AVG(pts))::numeric, 1) AS pts FROM")
        assert "first_date" not in sql
        assert "season = ANY" not in sql  # every season
        assert sql.endswith("GROUP BY player_id, name, season ORDER BY player_id, name, season LIMIT %s")
        assert c.params == (1, [JOKIC], "regular", True, ROW_CAP + 1)


@pytest.mark.unit
class TestCompiledSeasonView:
    def test_a_plain_season_question_reads_the_season_view(self):
        c = compiled(seasons=None, players=[JOKIC], grain="season", metrics=["pts", "gp", "td3", "fg_pct", "usg_pct"])
        sql = _one_line(c.sql)
        assert c.source == "seasons" and SEASONS_VIEW in sql and GAMES_VIEW not in sql
        assert sql.startswith("SELECT player_id, name, season, gp, ROUND((pts::numeric / NULLIF(gp, 0))::numeric, 1) AS pts, "
                              "td3 AS td3, ROUND((fgm::numeric / NULLIF(fga, 0))::numeric, 3) AS fg_pct, usg_pct AS usg_pct FROM")
        assert sql.endswith("WHERE player_id = ANY(%s) ORDER BY player_id, season LIMIT %s")
        assert c.params == ([JOKIC], ROW_CAP + 1)
        assert c.labels == {"gp": "games played", "pts": "points per game", "td3": "triple-doubles",
                            "fg_pct": "field goal %", "usg_pct": "usage %"}

    def test_totals_are_the_season_s_columns(self):
        c = compiled(seasons=["2024-25"], players=[JOKIC], grain="season", metrics=["pts"], agg="total")
        assert "pts AS pts" in _one_line(c.sql)
        assert c.labels["pts"] == "points, total"

    def test_season_leaders_qualify_on_gp_and_rank(self):
        c = compiled(seasons=["2024-25"], grain="season", metrics=["fpts"], rank={"by": "fpts", "top": 20}, min_games=50)
        sql = _one_line(c.sql)
        assert "WHERE season = ANY(%s) AND gp >= %s ORDER BY fpts DESC NULLS LAST, player_id, season LIMIT %s" in sql
        assert c.params == (["2024-25"], 50, 20)


@pytest.mark.unit
def test_every_statement_binds_its_values_and_names_only_catalog_identifiers():
    """No value from the spec reaches the SQL text; the row cap is a bound limit."""
    shapes = [
        dict(players=[JOKIC], grain="game", metrics=list(compiler.GAME_METRICS[:6]), transform={"rolling_mean": 7},
             where={"stats": [{"metric": "min", "op": ">", "value": 30.5}], "opponent": "LAL"}),
        dict(players=[JOKIC], grain="total", metrics=["pts"], window={"last_days": 14}),
        dict(grain="season", metrics=["pts"], rank={"by": "pts", "top": 25}, min_games=12),
        dict(players=[JOKIC], grain="season", metrics=["pts"], agg="max", seasons=["2023-24", "2024-25"]),
    ]
    for raw in shapes:
        s = spec(**raw)
        c = compile_spec(s, seasons=s.seasons or ["2025-26"], today=TODAY)
        for forbidden in ("203999", "LAL", "30.5", "2023", "2025-26", " 25 ", " 12 ", " 14 "):
            assert forbidden not in c.sql, (raw, forbidden)
        assert c.sql.count("%s") == len(c.params), raw
        assert c.params[-1] in (ROW_CAP + 1, 25)
