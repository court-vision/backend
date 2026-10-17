"""
Integration: the data behind query_stats (docs/QUERY_STATS_PLAN.md step 1).

Migration 0036's views against the real schema: game_no counts played games
per player, season and season type; the Cup final is a regular-season game;
opponent, home and won come from the game; a line without a game is left out;
a season row is history when history has the season and the latest snapshot
per player otherwise, with fpts from cv_core's formula. The metric catalog is
run against the views (drift: every expression selects), and against
hand-computed values (a rate is makes over attempts, a rolling mean is null
until its window is full). The reader runs read-only with a 2 s timeout on
either connection.

Step 2 runs real specs through run_query_stats against the same fixture: the
owner's rolling-mean chart, totals over the last N games, leaders counting
the games that passed a threshold, season rows from the view and from game
rows, coverage, the default season, and the row cap. Every shape the compiler
can produce is also run once, so a view or catalog change that breaks a
statement fails here.
"""

import asyncio
import os
from datetime import date
from decimal import Decimal

import pytest
from cv_core.transformers.fantasy_points import calculate_fantasy_points
from peewee import InternalError

from db.base import db
from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_history import PlayerHistory
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from services.ai.stats import catalog, query
from services.ai.stats.catalog import GAMES_VIEW, METRICS, SEASONS_VIEW
from services.ai.stats.compiler import GAME_METRICS, StatsSpec
from services.ai.stats.query import Coverage, SpecError, coverage, run_query_stats
from services.ai.stats.reader import StatsQueryTimeout, StatsReader

pytestmark = [pytest.mark.integration]

JOKIC, WEMBY, BENCH = 203999, 1641705, 999

# (game_id, date, home, away, home_score, away_score, status)
GAMES = [
    ("0022500001", date(2025, 10, 22), "DEN", "SAS", 110, 100, "final"),
    ("0022500002", date(2025, 10, 24), "MIA", "DEN", 105, 95, "final"),
    ("0022500003", date(2025, 10, 26), "DEN", "SAS", 120, 118, "final"),
    ("0022500004", date(2025, 10, 28), "DEN", "OKC", None, None, "in_progress"),
    ("0062500001", date(2025, 12, 16), "DEN", "SAS", 100, 90, "final"),   # the Cup final
    ("0042500101", date(2026, 4, 20), "DEN", "OKC", 101, 99, "final"),    # playoffs
    ("0022400010", date(2025, 3, 1), "DEN", "MIA", 130, 120, "final"),    # last season
]

# Jokic's lines, in date order: (game_id, min, pts, reb, ast, fgm, fga)
JOKIC_LINES = [
    ("0022500001", 35, 30, 12, 10, 12, 20),
    ("0022500002", 0, 0, 0, 0, 0, 0),      # on the sheet, never played
    ("0022500003", 38, 25, 15, 8, 10, 18),
    ("0022500004", 20, 14, 6, 5, 6, 11),
    ("0062500001", 36, 28, 11, 9, 11, 19),
    ("0042500101", 40, 35, 14, 12, 14, 24),
    ("0022400010", 34, 26, 13, 9, 11, 17),
]

HISTORY_2425 = dict(gp=70, min=Decimal("2400.5"), pts=2000, reb=900, ast=700, stl=120, blk=50, tov=200,
                    fgm=750, fga=1300, fg3m=100, fg3a=250, ftm=400, fta=500, oreb=200, dreb=700,
                    dd2=60, td3=30, usg_pct=Decimal("0.300"))

SNAPSHOT = dict(fpts=500, pts=300, reb=120, ast=100, stl=10, blk=8, tov=30, min=350,
                fgm=110, fga=200, fg3m=15, fg3a=40, ftm=65, fta=80)


def rows(sql, params=()):
    cur = db.execute_sql(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.fixture
def data(integration_db):
    for team in ("DEN", "SAS", "MIA", "OKC"):
        NBATeam.get_or_create(id=team, defaults={"name": team, "conference": "West", "division": "Northwest"})
    Player.create(id=JOKIC, name="Nikola Jokic", name_normalized="nikola jokic")
    Player.create(id=WEMBY, name="Victor Wembanyama", name_normalized="victor wembanyama")
    Player.create(id=BENCH, name="Bench Guy", name_normalized="bench guy")
    for game_id, day, home, away, hs, as_, status in GAMES:
        Game.create(game_id=game_id, game_date=day, season=f"20{game_id[3:5]}-{int(game_id[3:5]) + 1}",
                    home_team_id=home, away_team_id=away, home_score=hs, away_score=as_, status=status)
    by_id = {g[0]: g for g in GAMES}
    for game_id, mins, pts, reb, ast, fgm, fga in JOKIC_LINES:
        PlayerGameStats.upsert_game_stats(
            JOKIC, by_id[game_id][1],
            {"min": mins, "pts": pts, "reb": reb, "ast": ast, "fgm": fgm, "fga": fga,
             "fpts": calculate_fantasy_points({"pts": pts, "reb": reb, "ast": ast, "stl": 0, "blk": 0, "tov": 0,
                                                "fgm": fgm, "fga": fga, "fg3m": 0, "ftm": 0, "fta": 0})},
            team_id="DEN", game_id=game_id)
    for game_id, pts, blk in (("0022500001", 20, 4), ("0022500003", 22, 5)):
        PlayerGameStats.upsert_game_stats(WEMBY, by_id[game_id][1], {"min": 33, "pts": pts, "blk": blk, "fpts": pts},
                                          team_id="SAS", game_id=game_id)
    # A line with no game at all: no season, so no place in the view.
    PlayerGameStats.upsert_game_stats(BENCH, date(2025, 10, 22), {"min": 12, "pts": 4, "fpts": 4}, team_id="OKC")

    PlayerHistory.create(player_id=JOKIC, season="2024-25", player_name="Nikola Jokic", team="DEN", **HISTORY_2425)
    PlayerHistory.create(player_id=WEMBY, season="2024-25", player_name="Victor Wembanyama", team="SAS",
                         **{**HISTORY_2425, "gp": 46})
    # A player history knows and nba.players does not (retired before the players table existed).
    PlayerHistory.create(player_id=1, season="2012-13", player_name="Old Timer", team="BOS", **HISTORY_2425)
    # Two snapshots of this season for Jokic (the later one counts), one for Wemby,
    # and a stale snapshot of a season history already has (history wins).
    for as_of, gp in ((date(2025, 11, 1), 5), (date(2025, 11, 10), 10)):
        PlayerSeasonStats.create(player=JOKIC, team="DEN", as_of_date=as_of, season="2025-26", gp=gp, **SNAPSHOT)
    PlayerSeasonStats.create(player=WEMBY, team="SAS", as_of_date=date(2025, 11, 5), season="2025-26", gp=7, **SNAPSHOT)
    PlayerSeasonStats.create(player=WEMBY, team="SAS", as_of_date=date(2025, 4, 1), season="2024-25", gp=40, **SNAPSHOT)
    yield


class TestPlayerGamesView:
    def test_game_no_counts_played_games_per_player_season_and_type(self, data):
        got = rows(f"SELECT game_id, season, season_type, played, game_no, date FROM {GAMES_VIEW} "
                   "WHERE player_id = %s ORDER BY date", (JOKIC,))
        by_game = {r["game_id"]: r for r in got}
        assert len(got) == 7
        assert (by_game["0022500001"]["game_no"], by_game["0022500001"]["played"]) == (1, True)
        assert (by_game["0022500002"]["game_no"], by_game["0022500002"]["played"]) == (None, False)
        assert by_game["0022500003"]["game_no"] == 2  # the DNP did not take a number
        assert by_game["0022500004"]["game_no"] == 3
        # The Cup final is a regular-season game, next in the sequence.
        assert (by_game["0062500001"]["season_type"], by_game["0062500001"]["game_no"]) == ("regular", 4)
        # The playoffs and last season each start at 1.
        assert (by_game["0042500101"]["season_type"], by_game["0042500101"]["game_no"]) == ("playoffs", 1)
        assert (by_game["0022400010"]["season"], by_game["0022400010"]["game_no"]) == ("2024-25", 1)

    def test_opponent_home_and_result_come_from_the_game(self, data):
        got = {r["game_id"]: r for r in rows(
            f"SELECT game_id, team, opponent, home, won FROM {GAMES_VIEW} WHERE player_id = %s", (JOKIC,))}
        assert got["0022500001"] == {"game_id": "0022500001", "team": "DEN", "opponent": "SAS", "home": True, "won": True}
        assert got["0022500002"] == {"game_id": "0022500002", "team": "DEN", "opponent": "MIA", "home": False, "won": False}
        assert got["0022500004"]["won"] is None  # still in progress
        wemby = rows(f"SELECT opponent, home, won FROM {GAMES_VIEW} WHERE player_id = %s AND game_id = %s",
                     (WEMBY, "0022500001"))[0]
        assert wemby == {"opponent": "DEN", "home": False, "won": False}

    def test_a_line_without_a_game_has_no_season_and_is_left_out(self, data):
        assert rows(f"SELECT 1 FROM {GAMES_VIEW} WHERE player_id = %s", (BENCH,)) == []
        assert PlayerGameStats.select().where(PlayerGameStats.player == BENCH).count() == 1

    def test_the_name_and_box_score_ride_along(self, data):
        row = rows(f"SELECT name, min, pts, reb, ast, fgm, fga, fpts, oreb FROM {GAMES_VIEW} "
                   "WHERE player_id = %s AND game_id = %s", (JOKIC, "0022500001"))[0]
        assert row["name"] == "Nikola Jokic"
        assert (row["min"], row["pts"], row["reb"], row["ast"], row["fgm"], row["fga"]) == (35, 30, 12, 10, 12, 20)
        assert row["fpts"] == 30 + 12 + 20 + 24 - 20
        assert row["oreb"] is None  # a 0031 column the pipeline has not filled

    def test_a_filter_on_the_window_partition_is_pushed_below_the_window(self, data):
        """A per-player query must not number the whole table first."""
        plan = "\n".join(r["QUERY PLAN"] for r in rows(
            f"EXPLAIN SELECT game_no FROM {GAMES_VIEW} WHERE player_id = %s AND season = %s", (JOKIC, "2025-26")))
        assert "WindowAgg" in plan
        # The scan under the window carries the player filter.
        window_at = plan.index("WindowAgg")
        assert "player_id = 203999" in plan[window_at:]


class TestPlayerSeasonsView:
    def test_history_for_completed_seasons_with_fpts_from_the_formula(self, data):
        row = rows(f"SELECT * FROM {SEASONS_VIEW} WHERE player_id = %s AND season = %s", (JOKIC, "2024-25"))[0]
        assert row["source"] == "history"
        assert (row["name"], row["team"], row["gp"]) == ("Nikola Jokic", "DEN", 70)
        assert row["min"] == Decimal("2400.5")
        totals = {k: HISTORY_2425[k] for k in ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "ftm", "fta")}
        assert row["fpts"] == calculate_fantasy_points(totals)
        assert (row["dd2"], row["td3"], row["usg_pct"]) == (60, 30, Decimal("0.300"))
        assert (row["oreb"], row["dreb"]) == (200, 700)

    def test_the_latest_snapshot_per_player_stands_in_for_a_season_history_lacks(self, data):
        got = rows(f"SELECT player_id, source, gp, fpts, oreb, dd2 FROM {SEASONS_VIEW} WHERE season = %s "
                   "ORDER BY player_id", ("2025-26",))
        assert [(r["player_id"], r["source"], r["gp"]) for r in got] == [
            (JOKIC, "snapshot", 10), (WEMBY, "snapshot", 7)]
        assert got[0]["fpts"] == SNAPSHOT["fpts"]
        assert got[0]["oreb"] is None and got[0]["dd2"] is None  # the snapshot has neither

    def test_history_wins_when_both_hold_a_season(self, data):
        got = rows(f"SELECT source, gp FROM {SEASONS_VIEW} WHERE player_id = %s AND season = %s", (WEMBY, "2024-25"))
        assert got == [{"source": "history", "gp": 46}]

    def test_a_player_unknown_to_nba_players_keeps_his_history_name(self, data):
        got = rows(f"SELECT name, season FROM {SEASONS_VIEW} WHERE player_id = 1")
        assert got == [{"name": "Old Timer", "season": "2012-13"}]


class TestCatalogAgainstTheViews:
    """Drift: a view revision that renames or drops a column fails here, not in a user's turn."""

    def test_the_base_columns_exist(self, integration_db):
        def columns(view):
            return {r["column_name"] for r in rows(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'nba' AND table_name = %s",
                (view.split(".")[1],))}
        games, seasons = columns(GAMES_VIEW), columns(SEASONS_VIEW)
        assert set(catalog.GAMES_COLUMNS) <= games
        assert set(catalog.SEASONS_COLUMNS) <= seasons
        for m in METRICS.values():
            if m.in_games:
                assert set(m.columns()) <= games, m.name
            if m.in_seasons:
                assert set(m.columns()) <= seasons, m.name
        assert "gp" in seasons

    @pytest.mark.parametrize("name", list(METRICS))
    def test_every_expression_selects(self, integration_db, name):
        m = METRICS[name]
        frame = "OVER (PARTITION BY player_id, season, season_type ORDER BY date, game_id ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)"
        if m.in_games:
            rows(f"SELECT {m.game_sql()} FROM {GAMES_VIEW} LIMIT 0")
            rows(f"SELECT {m.frame_sql(frame)} FROM {GAMES_VIEW} LIMIT 0")
            for agg in ("per_game", "total", "max"):
                rows(f"SELECT player_id, {m.agg_sql(agg)} FROM {GAMES_VIEW} GROUP BY player_id LIMIT 0")
        if m.in_seasons:
            aggs = ("per_game", "total") if m.kind == "count" else ("per_game",)
            for agg in aggs:
                rows(f"SELECT {m.season_sql(agg)} FROM {SEASONS_VIEW} LIMIT 0")


class TestCatalogReferenceValues:
    def test_a_rate_over_games_is_makes_over_attempts(self, data):
        played = [l for l in JOKIC_LINES if l[0].startswith("0022500") and l[1] > 0] + [l for l in JOKIC_LINES if l[0].startswith("006")]
        expected = sum(l[5] for l in played) / sum(l[6] for l in played)
        got = rows(f"SELECT {METRICS['fg_pct'].agg_sql('per_game')} AS fg_pct, {METRICS['pts'].agg_sql('per_game')} AS ppg "
                   f"FROM {GAMES_VIEW} WHERE player_id = %s AND season = %s AND season_type = 'regular' AND played",
                   (JOKIC, "2025-26"))[0]
        assert float(got["fg_pct"]) == pytest.approx(expected)
        assert float(got["ppg"]) == pytest.approx(sum(l[2] for l in played) / len(played))
        # Averaging the per-game percentages would give a different number.
        assert expected != pytest.approx(sum(l[5] / l[6] for l in played) / len(played))

    def test_a_rolling_mean_is_null_until_its_window_is_full(self, data):
        k = 2
        frame = ("OVER (PARTITION BY player_id, season, season_type ORDER BY date, game_id "
                 f"ROWS BETWEEN {k - 1} PRECEDING AND CURRENT ROW)")
        got = rows(
            f"SELECT game_no, pts, CASE WHEN COUNT(*) {frame} >= {k} THEN {METRICS['pts'].frame_sql(frame)} END AS pts_rm, "
            f"{METRICS['fg_pct'].frame_sql(frame)} AS fg_rm "
            f"FROM {GAMES_VIEW} WHERE player_id = %s AND season = %s AND season_type = 'regular' AND played ORDER BY date",
            (JOKIC, "2025-26"))
        played = [l for l in JOKIC_LINES if l[0].startswith(("0022500", "006")) and l[1] > 0]
        assert [r["game_no"] for r in got] == [1, 2, 3, 4]
        assert got[0]["pts_rm"] is None
        for i in range(1, len(played)):
            window = played[i - k + 1:i + 1]
            assert float(got[i]["pts_rm"]) == pytest.approx(sum(l[2] for l in window) / k)
            assert float(got[i]["fg_rm"]) == pytest.approx(sum(l[5] for l in window) / sum(l[6] for l in window))

    def test_a_season_row_gives_per_game_from_totals_over_games_played(self, data):
        got = rows(f"SELECT {METRICS['pts'].season_sql('per_game')} AS ppg, {METRICS['pts'].season_sql('total')} AS pts, "
                   f"{METRICS['fg_pct'].season_sql()} AS fg_pct, {METRICS['gp'].season_sql()} AS gp "
                   f"FROM {SEASONS_VIEW} WHERE player_id = %s AND season = %s", (JOKIC, "2024-25"))[0]
        assert float(got["ppg"]) == pytest.approx(2000 / 70)
        assert (got["pts"], got["gp"]) == (2000, 70)
        assert float(got["fg_pct"]) == pytest.approx(750 / 1300)


class TestReader:
    def test_the_main_pool_fallback_is_read_only_with_the_short_timeout(self, integration_db):
        reader = StatsReader(None)
        assert reader.mode == "main_pool"
        got = asyncio.run(reader.query(
            "SELECT current_setting('transaction_read_only') AS ro, current_setting('statement_timeout') AS timeout"))
        assert got == [{"ro": "on", "timeout": "2s"}]
        with pytest.raises(InternalError, match="read-only transaction"):
            asyncio.run(reader.query("CREATE TEMP TABLE should_not_exist (x int)"))
        # The transaction has ended: the connection is back to the pool's defaults.
        after = asyncio.run(reader.query("SELECT 1 AS one"))
        assert after == [{"one": 1}]
        assert rows("SELECT current_setting('statement_timeout') AS t")[0]["t"] == "30s"

    def test_a_slow_query_is_cut_off_and_named_as_such(self, integration_db):
        with pytest.raises(StatsQueryTimeout):
            asyncio.run(StatsReader(None).query("SELECT pg_sleep(2.5)"))

    def test_the_dedicated_pool_carries_the_same_bounds(self, integration_db):
        reader = StatsReader(os.environ["DATABASE_URL"])
        assert reader.mode == "ask_reader"
        try:
            got = asyncio.run(reader.query(
                "SELECT current_setting('default_transaction_read_only') AS ro, "
                "current_setting('statement_timeout') AS timeout, %s AS echoed", ("x",)))
            assert got == [{"ro": "on", "timeout": "2s", "echoed": "x"}]
            with pytest.raises(StatsQueryTimeout):
                asyncio.run(reader.query("SELECT pg_sleep(2.5)"))
        finally:
            reader.pool.close_all()


# ---- step 2: query_stats end to end ----

TODAY = date(2026, 1, 15)
# Jokic's regular-season 2025-26 lines the views count (the DNP is out, the Cup final is in), in order:
# fpts = pts + reb + 2 ast + 2 fgm - fga with the other inputs at zero.
JOKIC_REGULAR = [l for l in JOKIC_LINES if l[0].startswith(("0022500", "006")) and l[1] > 0]
JOKIC_FPTS = [l[2] + l[3] + 2 * l[4] + 2 * l[5] - l[6] for l in JOKIC_REGULAR]  # 66, 58, 31, 60


def ask(reader, **raw):
    return asyncio.run(run_query_stats(StatsSpec.model_validate(raw), reader=reader, today=TODAY))


def chunks(names, n=6):
    names = list(names)
    return [names[i:i + n] for i in range(0, len(names), n)]


@pytest.fixture
def reader(data):
    query.reset_coverage_cache()
    yield StatsReader(None)
    query.reset_coverage_cache()


class TestRunQueryStats:
    def test_coverage_reads_the_seasons_with_games(self, reader):
        assert asyncio.run(coverage(reader)) == Coverage("2024-25", "2025-26")
        with pytest.raises(SpecError, match="covers 2024-25 to 2025-26; 2012-13 is outside it"):
            ask(reader, players=[JOKIC], grain="game", metrics=["pts"], seasons=["2012-13"])

    def test_the_owner_s_question_a_rolling_mean_per_player(self, reader):
        out = ask(reader, players=[WEMBY, JOKIC], grain="game", metrics=["fpts"], transform={"rolling_mean": 2})
        assert out["seasons"] == ["2025-26"]  # the default: the latest season with games
        assert out["labels"] == {"fpts_rm2": "CV fantasy points, 2-game rolling mean"}
        jokic = [r for r in out["rows"] if r["player_id"] == JOKIC]
        wemby = [r for r in out["rows"] if r["player_id"] == WEMBY]
        assert [r["game_no"] for r in jokic] == [1, 2, 3, 4] and [r["fpts"] for r in jokic] == JOKIC_FPTS
        assert [r["fpts_rm2"] for r in jokic] == [None] + [round((a + b) / 2, 1) for a, b in zip(JOKIC_FPTS, JOKIC_FPTS[1:])]
        assert [r["date"] for r in jokic] == ["2025-10-22", "2025-10-26", "2025-10-28", "2025-12-16"]
        assert [r["fpts"] for r in wemby] == [20, 22] and [r["fpts_rm2"] for r in wemby] == [None, 21.0]
        assert {r["series"] for r in jokic} == {"Nikola Jokic"} and {r["series"] for r in wemby} == {"Victor Wembanyama"}
        assert out["rows"][0]["opponent"] == "SAS" and out["rows"][0]["home"] is True
        assert out["note"] == catalog.FPTS_NOTE

    def test_totals_over_the_last_games(self, reader):
        out = ask(reader, players=[JOKIC], grain="total", metrics=["pts", "fg_pct", "gp"], window={"last_games": 2})
        last_two = JOKIC_REGULAR[-2:]
        assert out["rows"] == [{"player_id": JOKIC, "name": "Nikola Jokic", "gp": 2, "first_date": "2025-10-28",
                                "last_date": "2025-12-16", "pts": sum(l[2] for l in last_two) / 2,
                                "fg_pct": round(sum(l[5] for l in last_two) / sum(l[6] for l in last_two), 3)}]
        assert out["window"] == {"last_games": 2} and out["agg"] == "per_game"
        assert out["labels"] == {"gp": "games played", "pts": "points per game", "fg_pct": "field goal %"}

    def test_leaders_count_the_games_over_a_threshold(self, reader):
        out = ask(reader, grain="total", metrics=["pts"], agg="count_games",
                  where={"stats": [{"metric": "pts", "op": ">=", "value": 25}]}, rank={"by": "gp", "top": 5})
        assert [(r["name"], r["gp"]) for r in out["rows"]] == [("Nikola Jokic", 3)]  # Wemby never got there
        assert out["min_games"] == 1 and out["rows"][0]["pts"] == pytest.approx((30 + 25 + 28) / 3, abs=0.05)

    def test_leaders_qualify_on_games_played(self, reader):
        out = ask(reader, grain="total", metrics=["pts"], rank={"by": "pts"})
        assert out["rows"] == [] and out["note"] == "No rows matched" and out["min_games"] == 10
        out = ask(reader, grain="total", metrics=["pts"], rank={"by": "pts"}, min_games=2)
        # 97 / 4 = 24.25: Postgres rounds half away from zero
        assert [(r["name"], r["pts"]) for r in out["rows"]] == [("Nikola Jokic", 24.3), ("Victor Wembanyama", 21.0)]

    def test_season_rows_come_from_the_season_view_when_nothing_narrows_them(self, reader):
        out = ask(reader, players=[JOKIC], grain="season", metrics=["pts", "fg_pct", "dd2", "td3"])
        assert out["seasons"] == "all" and out["agg"] == "per_game"
        assert [(r["season"], r["gp"], r["pts"], r["dd2"]) for r in out["rows"]] == [
            ("2024-25", 70, round(2000 / 70, 1), 60),      # history
            ("2025-26", 10, 30.0, None),                  # the latest snapshot
        ]
        assert out["rows"][0]["fg_pct"] == round(750 / 1300, 3)
        assert out["labels"]["dd2"] == "double-doubles"

    def test_season_rows_are_grouped_from_games_when_a_split_narrows_them(self, reader):
        out = ask(reader, players=[JOKIC], grain="season", metrics=["pts"], where={"home": True})
        assert [(r["season"], r["gp"], r["pts"]) for r in out["rows"]] == [("2024-25", 1, 26.0), ("2025-26", 4, 24.3)]
        out = ask(reader, players=[JOKIC], grain="season", metrics=["pts"], where={"home": False})
        assert out["rows"] == [] and out["note"] == f"No regular games for player_id {JOKIC}"

    def test_a_mean_runs_from_the_season_s_start_whatever_the_window(self, reader):
        out = ask(reader, players=[JOKIC], grain="game", metrics=["pts"], transform={"cumulative_mean": True},
                  window={"from": "2025-10-27"})
        # Games 3 and 4 come back, and their means still count games 1 and 2 (30, 25, 14, 28 points)
        assert [(r["game_no"], r["pts_cum"]) for r in out["rows"]] == [(3, 23.0), (4, 24.3)]
        out = ask(reader, players=[JOKIC], grain="game", metrics=["pts"], transform={"rolling_mean": 2},
                  window={"last_days": 60})
        assert [(r["game_no"], r["pts_rm2"]) for r in out["rows"]] == [(4, 21.0)]

    def test_the_best_stretch_ranks_games_by_the_transform_column(self, reader):
        out = ask(reader, players=[JOKIC], grain="game", metrics=["fpts"], transform={"rolling_mean": 2},
                  rank={"by": "fpts_rm2", "top": 1})
        assert [(r["game_no"], r["fpts_rm2"]) for r in out["rows"]] == [(2, (JOKIC_FPTS[0] + JOKIC_FPTS[1]) / 2)]

    def test_another_season_type_and_a_player_without_games_there(self, reader):
        out = ask(reader, players=[JOKIC, WEMBY], grain="game", metrics=["pts"], season_type="playoffs")
        assert [(r["name"], r["game_no"], r["pts"]) for r in out["rows"]] == [("Nikola Jokic", 1, 35)]
        assert out["note"] == f"No playoffs games in 2025-26 for player_id {WEMBY}"

    def test_a_sub_minute_appearance_does_not_count_and_min_minutes_narrows_further(self, reader):
        out = ask(reader, players=[JOKIC], grain="total", metrics=["pts"])
        assert out["rows"][0]["gp"] == 4  # the 0-minute line is out
        out = ask(reader, players=[JOKIC], grain="total", metrics=["pts"], where={"min_minutes": 36})
        assert out["rows"][0]["gp"] == 2  # 38 and 36 minutes

    def test_the_row_cap_is_a_readable_refusal(self, reader, monkeypatch):
        monkeypatch.setattr(query, "ROW_CAP", 2)
        with pytest.raises(SpecError, match="Over 2 rows. Narrow it"):
            ask(reader, players=[JOKIC], grain="game", metrics=["pts"])


class TestEveryShapeRuns:
    """Each statement the compiler can produce runs against the migrated views."""

    season_metrics = [n for n, m in METRICS.items() if m.in_seasons]

    @pytest.mark.parametrize("metrics", chunks(GAME_METRICS))
    @pytest.mark.parametrize("transform", [None, {"rolling_mean": 3}, {"cumulative_mean": True}])
    def test_game_rows(self, reader, metrics, transform):
        out = ask(reader, players=[JOKIC], grain="game", metrics=metrics, transform=transform, season_type="all")
        assert out["total_rows"] == 5  # this season's four regular games and the playoff game

    @pytest.mark.parametrize("metrics", chunks(GAME_METRICS))
    @pytest.mark.parametrize("agg", ["per_game", "total", "max"])
    def test_totals_and_seasons_from_game_rows(self, reader, metrics, agg):
        assert ask(reader, players=[JOKIC], grain="total", metrics=metrics, agg=agg, window={"last_days": 365})["total_rows"] == 1
        assert ask(reader, players=[JOKIC], grain="season", metrics=metrics, agg=agg, season_type="all")["total_rows"] == 2

    @pytest.mark.parametrize("metrics", chunks(season_metrics))
    @pytest.mark.parametrize("agg", ["per_game", "total"])
    def test_season_view(self, reader, metrics, agg):
        assert ask(reader, players=[JOKIC, WEMBY], grain="season", metrics=metrics, agg=agg)["total_rows"] == 4

    def test_leaders_in_each_shape(self, reader):
        ask(reader, grain="total", metrics=["fpts"], window={"last_games": 2}, rank={"by": "fpts", "top": 3})
        ask(reader, grain="season", metrics=["fpts", "gp"], rank={"by": "gp", "order": "asc"}, min_games=1)
        ask(reader, grain="season", metrics=["pts"], agg="max", rank={"by": "pts"}, min_games=1)
        ask(reader, grain="total", metrics=["blk"], agg="count_games",
            where={"stats": [{"metric": "blk", "op": ">=", "value": 4}], "opponent": "DEN", "home": False},
            rank={"by": "gp"})
