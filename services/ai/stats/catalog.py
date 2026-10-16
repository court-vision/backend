"""
The metric catalog: one entry per stat query_stats can name, with the SQL that
computes it at each grain over the two views migration 0036 created.

Every derived number the tool returns is computed here or nowhere: a rate is
makes over attempts for the rows in question (one game, a group of games, a
window frame), never an average of per-game percentages, and a per-game
average over a season row is the total over games played. The compiler
(step 2) assembles statements from these expressions and the views' base
columns; it never writes a metric expression of its own, so adding a stat is
one entry here plus its reference test.

The catalog is also what the tool description lists (names and labels, no
SQL), so the model and the server always agree on the vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cv_core.scoring_vocab import STATS

GAMES_VIEW = "nba.ask_player_games"
SEASONS_VIEW = "nba.ask_player_seasons"

# The views' non-metric columns, as migration 0036 names them. The compiler
# selects these by name; the integration drift test checks the lists against
# the migrated schema, so a view revision that renames one fails CI.
GAMES_COLUMNS: tuple[str, ...] = (
    "player_id", "name", "team", "season", "season_type", "date", "game_id",
    "opponent", "home", "won", "played", "game_no",
)
SEASONS_COLUMNS: tuple[str, ...] = ("player_id", "name", "season", "team", "source")

Kind = Literal["count", "rate", "season"]
Agg = Literal["per_game", "total", "max"]

AGG_FUNCTIONS: dict[str, str] = {"per_game": "AVG", "total": "SUM", "max": "MAX"}


@dataclass(frozen=True)
class Metric:
    """One stat the tool can name.

    count:  a box-score column, summed or averaged over games.
    rate:   makes over attempts, both count columns.
    season: a figure only the season view carries (gp, dd2, td3, usg_pct).

    `in_games` and `in_seasons` say which view carries the columns it needs.
    """

    name: str
    label: str
    kind: Kind
    column: str | None = None
    makes: str | None = None
    attempts: str | None = None
    in_games: bool = True
    in_seasons: bool = True
    # True when a NULL is expected on some rows (columns added in 0031, or
    # that the season snapshot lacks), so a reader shows a gap, not a zero.
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.kind == "rate":
            if not (self.makes and self.attempts) or self.column:
                raise ValueError(f"{self.name}: a rate names makes and attempts, nothing else")
        elif not self.column or self.makes or self.attempts:
            raise ValueError(f"{self.name}: a {self.kind} metric names one column")
        if self.kind == "season" and self.in_games:
            raise ValueError(f"{self.name}: a season-only metric is not on the games view")

    # -- SQL. `t` is the table alias in the statement ("" for none). Every
    # identifier comes from this module; values never appear here.

    def game_sql(self, t: str = "") -> str:
        """The metric on one game row (grain game)."""
        self._needs("game", self.in_games)
        if self.kind == "rate":
            return f"{_q(t, self.makes)}::numeric / NULLIF({_q(t, self.attempts)}, 0)"
        return _q(t, self.column)

    def agg_sql(self, agg: str, t: str = "") -> str:
        """The metric over a group of game rows (grain total, or season from games)."""
        self._needs(f"{agg} over games", self.in_games)
        if self.kind == "rate":
            # Whatever the agg, a rate over several games is makes over attempts.
            return f"SUM({_q(t, self.makes)})::numeric / NULLIF(SUM({_q(t, self.attempts)}), 0)"
        fn = AGG_FUNCTIONS[agg]
        return f"{fn}({_q(t, self.column)})"

    def frame_sql(self, frame: str, t: str = "") -> str:
        """A rolling or cumulative mean over a window frame (grain game, transform).

        `frame` is the full OVER clause, "OVER (PARTITION BY ... ROWS BETWEEN ...)".
        """
        self._needs("a window", self.in_games)
        if self.kind == "rate":
            return (f"SUM({_q(t, self.makes)}) {frame}::numeric"
                    f" / NULLIF(SUM({_q(t, self.attempts)}) {frame}, 0)")
        return f"AVG({_q(t, self.column)}) {frame}"

    def season_sql(self, agg: str = "per_game", t: str = "") -> str:
        """The metric on one season row (grain season from the seasons view)."""
        self._needs(f"{agg} over a season row", self.in_seasons)
        if self.kind == "rate":
            return f"{_q(t, self.makes)}::numeric / NULLIF({_q(t, self.attempts)}, 0)"
        if self.kind == "season":
            return _q(t, self.column)
        if agg == "per_game":
            return f"{_q(t, self.column)}::numeric / NULLIF({_q(t, 'gp')}, 0)"
        if agg == "total":
            return _q(t, self.column)
        raise ValueError(f"{self.name}: no {agg} over a season row (it holds totals, not games)")

    def columns(self) -> tuple[str, ...]:
        """The view columns the metric reads."""
        if self.kind == "rate":
            return (self.makes, self.attempts)  # type: ignore[return-value]
        return (self.column,)  # type: ignore[return-value]

    def _needs(self, what: str, available: bool) -> None:
        if not available:
            raise ValueError(f"{self.name} has no {what}")


def _q(t: str, column: str | None) -> str:
    return f"{t}.{column}" if t else str(column)


def _count(name: str, label: str, **kw) -> Metric:
    return Metric(name, label, "count", column=name, **kw)


def _rate(name: str, label: str) -> Metric:
    stat = STATS[name]
    return Metric(name, label, "rate", makes=stat.numerator, attempts=stat.denominator)


def _season(name: str, label: str, **kw) -> Metric:
    return Metric(name, label, "season", column=name, in_games=False, **kw)


_METRICS: tuple[Metric, ...] = (
    _count("pts", "points"),
    _count("reb", "rebounds"),
    _count("ast", "assists"),
    _count("stl", "steals"),
    _count("blk", "blocks"),
    _count("tov", "turnovers"),
    _count("fgm", "field goals made"),
    _count("fga", "field goals attempted"),
    _rate("fg_pct", "field goal %"),
    _count("fg3m", "threes made"),
    _count("fg3a", "threes attempted"),
    _rate("fg3_pct", "three point %"),
    _count("ftm", "free throws made"),
    _count("fta", "free throws attempted"),
    _rate("ft_pct", "free throw %"),
    _count("oreb", "offensive rebounds", nullable=True),
    _count("dreb", "defensive rebounds", nullable=True),
    _count("pf", "personal fouls", in_seasons=False, nullable=True),
    _count("plus_minus", "plus/minus", in_seasons=False, nullable=True),
    _count("min", "minutes"),
    _count("fpts", "CV fantasy points"),
    _season("gp", "games played"),
    _season("dd2", "double-doubles", nullable=True),
    _season("td3", "triple-doubles", nullable=True),
    _season("usg_pct", "usage %", nullable=True),
)

METRICS: dict[str, Metric] = {m.name: m for m in _METRICS}
METRIC_NAMES: tuple[str, ...] = tuple(METRICS)

# A note the tool result carries whenever fpts is among the metrics: the
# formula is Court Vision's, not the user's league scoring.
FPTS_NOTE = "fpts is Court Vision's fantasy points formula, not the user's league scoring"


def describe() -> str:
    """The catalog as the tool description lists it: `name (label)`, comma-separated."""
    return ", ".join(f"{m.name} ({m.label})" for m in _METRICS)


def label(name: str, *, rolling: int | None = None, cumulative: bool = False) -> str:
    """A result column's label, e.g. "CV fantasy points, 5-game rolling mean"."""
    base = METRICS[name].label
    if rolling:
        return f"{base}, {rolling}-game rolling mean"
    if cumulative:
        return f"{base}, season-to-date mean"
    return base
