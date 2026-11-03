"""
The metric catalog behind query_stats (services/ai/stats/catalog.py): every
stat the tool can name, and the SQL that computes it at each grain. The
expressions are checked against the real views in
tests/integration/test_ai_stats_views_integration.py; here it is the shape of
the catalog and what each kind of metric compiles to.
"""

import pytest

from services.ai.stats import catalog
from services.ai.stats.catalog import METRICS, Metric


@pytest.mark.unit
class TestCatalog:
    def test_names_are_the_keys_and_unique(self):
        assert list(METRICS) == [m.name for m in METRICS.values()]
        assert len(set(METRICS)) == len(METRICS)
        assert catalog.METRIC_NAMES == tuple(METRICS)

    def test_the_box_score_and_the_season_figures_are_all_there(self):
        for name in ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a",
                     "ftm", "fta", "min", "fpts", "fg_pct", "ft_pct", "fg3_pct"):
            assert METRICS[name].in_games and METRICS[name].in_seasons, name
        for name in ("gp", "dd2", "td3", "usg_pct"):
            assert METRICS[name].kind == "season" and not METRICS[name].in_games, name
        for name in ("pf", "plus_minus"):
            assert METRICS[name].in_games and not METRICS[name].in_seasons, name

    def test_a_rate_is_makes_over_attempts_from_the_shared_vocabulary(self):
        assert (METRICS["fg_pct"].makes, METRICS["fg_pct"].attempts) == ("fgm", "fga")
        assert (METRICS["ft_pct"].makes, METRICS["ft_pct"].attempts) == ("ftm", "fta")
        assert (METRICS["fg3_pct"].makes, METRICS["fg3_pct"].attempts) == ("fg3m", "fg3a")
        for m in METRICS.values():
            if m.kind == "rate":
                assert METRICS[m.makes].kind == "count" and METRICS[m.attempts].kind == "count"

    def test_fpts_is_labelled_as_court_visions_formula(self):
        assert METRICS["fpts"].label == "CV fantasy points"
        assert "not the user's league scoring" in catalog.FPTS_NOTE

    def test_the_description_names_every_metric_once(self):
        text = catalog.describe()
        for name, m in METRICS.items():
            assert text.count(f"{name} ({m.label})") == 1
        assert "SELECT" not in text and "(" in text

    def test_labels_for_transformed_columns(self):
        assert catalog.label("fpts") == "CV fantasy points"
        assert catalog.label("fpts", rolling=5) == "CV fantasy points, 5-game rolling mean"
        assert catalog.label("pts", cumulative=True) == "points, season-to-date mean"


@pytest.mark.unit
class TestSql:
    FRAME = "OVER (PARTITION BY player_id ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)"

    def test_a_count_is_its_column_at_every_grain(self):
        pts = METRICS["pts"]
        assert pts.game_sql() == "pts"
        assert pts.game_sql("g") == "g.pts"
        assert pts.agg_sql("per_game") == "AVG(pts)"
        assert pts.agg_sql("total", "g") == "SUM(g.pts)"
        assert pts.agg_sql("max") == "MAX(pts)"
        assert pts.frame_sql(self.FRAME) == f"AVG(pts) {self.FRAME}"
        assert pts.season_sql("per_game") == "pts::numeric / NULLIF(gp, 0)"
        assert pts.season_sql("total", "s") == "s.pts"
        assert pts.columns() == ("pts",)

    def test_a_rate_is_never_an_average_of_percentages(self):
        fg = METRICS["fg_pct"]
        assert fg.game_sql() == "fgm::numeric / NULLIF(fga, 0)"
        for agg in ("per_game", "total", "max"):
            assert fg.agg_sql(agg) == "SUM(fgm)::numeric / NULLIF(SUM(fga), 0)"
        assert fg.frame_sql(self.FRAME) == (
            f"SUM(fgm) {self.FRAME}::numeric / NULLIF(SUM(fga) {self.FRAME}, 0)"
        )
        assert fg.season_sql() == "fgm::numeric / NULLIF(fga, 0)"
        assert fg.columns() == ("fgm", "fga")
        for m in METRICS.values():
            if m.kind == "rate":
                assert "AVG" not in m.agg_sql("per_game") and "AVG" not in m.frame_sql(self.FRAME)

    def test_a_season_figure_is_read_as_stored_and_has_no_game_form(self):
        gp = METRICS["gp"]
        assert gp.season_sql() == "gp"
        assert gp.season_sql("total", "s") == "s.gp"
        with pytest.raises(ValueError, match="gp has no game"):
            gp.game_sql()
        with pytest.raises(ValueError, match="gp has no"):
            gp.frame_sql(self.FRAME)

    def test_a_metric_the_season_view_lacks_refuses_the_season_form(self):
        with pytest.raises(ValueError, match="plus_minus has no"):
            METRICS["plus_minus"].season_sql()
        assert METRICS["plus_minus"].agg_sql("per_game") == "AVG(plus_minus)"

    def test_a_season_row_holds_totals_so_max_has_no_meaning_there(self):
        with pytest.raises(ValueError, match="no max over a season row"):
            METRICS["pts"].season_sql("max")

    def test_expressions_name_only_catalog_columns(self):
        """Identifiers come from this module and the views; values never do."""
        import re

        known = set(catalog.GAMES_COLUMNS) | set(catalog.SEASONS_COLUMNS) | {m.column for m in METRICS.values() if m.column}
        for m in METRICS.values():
            forms = []
            if m.in_games:
                forms += [m.game_sql(), m.agg_sql("per_game"), m.frame_sql("OVER (w)")]
            if m.in_seasons:
                forms += [m.season_sql("total" if m.kind == "count" else "per_game")]
            for sql in forms:
                words = set(re.findall(r"[a-z_][a-z0-9_]*", sql.lower()))
                words -= {"avg", "sum", "max", "nullif", "numeric", "over", "w"}
                assert words <= known, (m.name, sql, words - known)


@pytest.mark.unit
class TestMetricShape:
    def test_a_rate_names_makes_and_attempts_and_nothing_else(self):
        with pytest.raises(ValueError, match="makes and attempts"):
            Metric("x", "x", "rate", column="x")
        with pytest.raises(ValueError, match="makes and attempts"):
            Metric("x", "x", "rate", makes="a")

    def test_a_count_names_one_column(self):
        with pytest.raises(ValueError, match="one column"):
            Metric("x", "x", "count")
        with pytest.raises(ValueError, match="one column"):
            Metric("x", "x", "count", column="x", makes="a")

    def test_a_season_only_metric_cannot_claim_the_games_view(self):
        with pytest.raises(ValueError, match="not on the games view"):
            Metric("x", "x", "season", column="x", in_games=True)
