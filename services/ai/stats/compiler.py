"""
The spec compiler: the small spec the model writes for query_stats, validated,
and the SQL it becomes over the two nba.ask_* views (docs/QUERY_STATS_PLAN.md).

Identifiers in the SQL come only from the catalog and the views; every value
the model supplies (ids, seasons, thresholds, dates, limits) is a bound
parameter. The model never writes SQL, so the traps (averaged percentages,
summed snapshots, sub-minute appearances counted as games) are closed here and
in the catalog, once.

Shapes:

- **game**: one row per game played, in date order, with the metrics for that
  game and, with a transform, a rolling or season-to-date mean over the
  player's played games in that season (null until a rolling window is full).
- **total**: one row per player over the window, grouped from game rows: `gp`,
  the window's first and last dates, and each metric aggregated (per_game,
  total or max; count_games is `gp` over the rows that passed `where`).
- **season**: one row per player-season. From `ask_player_seasons` (season
  totals, per game or total) when nothing narrows the season; grouped from
  game rows when a window, a filter, another season type or `max` does.
- **leaders**: the total or season shape over every player, keeping those with
  at least `min_games`, ordered by `rank`.

The validators raise ValueError with the fix in the message: run_tool turns
them into "Invalid input: ..." for the model to read and correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.season import validate_season
from services.ai.stats.catalog import GAMES_VIEW, METRICS, SEASONS_VIEW, Metric
from services.ai.stats.catalog import label as metric_label

ROW_CAP = 400
MAX_PLAYERS = 8
MAX_METRICS = 6
MAX_SEASONS = 15
MAX_TOP = 50
DEFAULT_MIN_GAMES = 10
RATE_PLACES = 3
MEAN_PLACES = 1

Grain = Literal["game", "total", "season"]
SeasonType = Literal["regular", "playoffs", "play_in", "all"]
Agg = Literal["per_game", "total", "max", "count_games"]
Op = Literal[">=", ">", "<=", "<", "=="]
_SQL_OPS: dict[str, str] = {">=": ">=", ">": ">", "<=": "<=", "<": "<", "==": "="}

GAME_METRICS: tuple[str, ...] = tuple(n for n, m in METRICS.items() if m.in_games)
SEASON_ONLY_METRICS: tuple[str, ...] = tuple(n for n, m in METRICS.items() if m.kind == "season")
# Columns every game row carries ahead of its metrics
GAME_ROW_COLUMNS: tuple[str, ...] = ("player_id", "name", "season", "season_type", "game_no", "date", "opponent", "home")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatFilter(_Strict):
    """One threshold on a game's stat: `{"metric": "pts", "op": ">=", "value": 40}`."""

    metric: str
    op: Op
    value: float

    @field_validator("metric")
    @classmethod
    def _a_game_stat(cls, v: str) -> str:
        m = METRICS.get(v)
        if m is None or not m.in_games:
            raise ValueError(f"'{v}' is not a game stat; one of {', '.join(GAME_METRICS)}")
        return v


class Where(_Strict):
    stats: list[StatFilter] = Field(default_factory=list, max_length=6)
    opponent: Optional[str] = Field(None, pattern=r"^[A-Z]{2,3}$")
    home: Optional[bool] = None
    # Games with fewer minutes do not count. 1 is "played": a sub-minute
    # appearance is a row every reader skips.
    min_minutes: int = Field(1, ge=1, le=60)


class Window(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    last_games: Optional[int] = Field(None, ge=1, le=82)
    last_days: Optional[int] = Field(None, ge=1, le=366)
    from_date: Optional[date] = Field(None, alias="from")
    to_date: Optional[date] = Field(None, alias="to")

    @model_validator(mode="after")
    def _one_form(self) -> "Window":
        forms = [self.last_games is not None, self.last_days is not None,
                 self.from_date is not None or self.to_date is not None]
        if sum(forms) != 1:
            raise ValueError("window is one of last_games, last_days, or from/to dates")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("window: from is after to")
        return self


class Transform(_Strict):
    rolling_mean: Optional[int] = Field(None, ge=2, le=20)
    cumulative_mean: Optional[bool] = None

    @model_validator(mode="after")
    def _one_form(self) -> "Transform":
        if (self.rolling_mean is None) == (not self.cumulative_mean):
            raise ValueError("transform is rolling_mean (2-20 games) or cumulative_mean true, one of them")
        return self

    @property
    def suffix(self) -> str:
        return f"_rm{self.rolling_mean}" if self.rolling_mean else "_cum"


class Rank(_Strict):
    by: str
    top: int = Field(10, ge=1, le=MAX_TOP)
    order: Literal["desc", "asc"] = "desc"


class StatsSpec(_Strict):
    players: Optional[list[int]] = Field(None, min_length=1, max_length=MAX_PLAYERS)
    grain: Grain
    metrics: list[str] = Field(..., min_length=1, max_length=MAX_METRICS)
    agg: Agg = "per_game"
    seasons: Optional[list[str]] = Field(None, min_length=1, max_length=MAX_SEASONS)
    season_type: SeasonType = "regular"
    window: Optional[Window] = None
    where: Optional[Where] = None
    transform: Optional[Transform] = None
    rank: Optional[Rank] = None
    min_games: Optional[int] = Field(None, ge=1, le=82)

    @field_validator("players")
    @classmethod
    def _player_ids(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is not None:
            if any(p <= 0 for p in v):
                raise ValueError("player ids are positive NBA player_ids from search_players")
            if len(set(v)) != len(v):
                raise ValueError("a player is listed twice")
        return v

    @field_validator("metrics")
    @classmethod
    def _known_metrics(cls, v: list[str]) -> list[str]:
        unknown = [m for m in v if m not in METRICS]
        if unknown:
            raise ValueError(f"unknown metric {', '.join(repr(m) for m in unknown)}; one of {', '.join(METRICS)}")
        if len(set(v)) != len(v):
            raise ValueError("a metric is listed twice")
        return v

    @field_validator("seasons")
    @classmethod
    def _season_keys(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for s in v:
                try:
                    validate_season(s)
                except ValueError:
                    raise ValueError(f"season {s!r} should look like '2025-26'") from None
            if len(set(v)) != len(v):
                raise ValueError("a season is listed twice")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "StatsSpec":
        leaders = self.players is None
        if self.grain == "game":
            if self.agg != "per_game":
                raise ValueError("agg applies to grain total or season; grain game returns each game as it was")
            if leaders:
                raise ValueError("grain game needs players; for leaders use grain total or season with rank")
            not_per_game = [m for m in self.metrics if not METRICS[m].in_games]
            if not_per_game:
                raise ValueError(f"{', '.join(not_per_game)}: a season figure, not a game stat; use grain season")
        else:
            if self.transform is not None:
                raise ValueError("transform (rolling_mean, cumulative_mean) applies to grain game only")
        if self.grain == "total":
            season_only = [m for m in self.metrics if METRICS[m].kind == "season" and m != "gp"]
            if season_only:
                raise ValueError(f"{', '.join(season_only)} is only on whole seasons: use grain season")
        if self.grain == "season" and self.agg == "max" and any(METRICS[m].kind == "season" for m in self.metrics
                                                                  if m != "gp"):
            raise ValueError("max applies to game stats, not to gp, dd2, td3 or usg_pct")
        if self.grain == "season" and self._from_game_rows():
            season_only = [m for m in self.metrics if METRICS[m].kind == "season" and m != "gp"]
            if season_only:
                raise ValueError(f"{', '.join(season_only)} is only on whole regular seasons: drop where, window "
                                 "and season_type, or use game stats")
        if self.agg == "count_games" and self.grain == "game":
            raise ValueError("count_games applies to grain total or season")
        if leaders:
            if self.rank is None:
                raise ValueError("without players, give rank (by, top) for league-wide leaders")
        elif self.min_games is not None:
            raise ValueError("min_games is for leaders: omit players")
        if self.rank is not None:
            rankable = set(self.metrics)
            if self.grain != "game":
                rankable.add("gp")
            elif self.transform is not None:
                rankable.update(f"{m}{self.transform.suffix}" for m in self.metrics)
            if self.rank.by not in rankable:
                raise ValueError(f"rank.by must be one of the metrics ({', '.join(sorted(rankable))})")
            if self.agg == "count_games" and self.rank.by != "gp":
                raise ValueError("with count_games, rank by 'gp': the number of games that passed where")
        return self

    # -- what the compiler reads

    def _from_game_rows(self) -> bool:
        """Season grain: whether the season view will not do and game rows are grouped instead."""
        if self.grain != "season":
            return True
        if self.where is not None or self.window is not None or self.season_type != "regular":
            return True
        if self.agg in ("max", "count_games"):
            return True
        return any(not METRICS[m].in_seasons for m in self.metrics)

    @property
    def leaders(self) -> bool:
        return self.players is None

    def effective_min_games(self) -> int:
        if self.min_games is not None:
            return self.min_games
        if self.agg == "count_games":
            # Counting the games that passed `where`: one such game qualifies
            return 1
        if self.window is not None and self.window.last_games is not None:
            return min(DEFAULT_MIN_GAMES, self.window.last_games)
        return DEFAULT_MIN_GAMES


@dataclass(frozen=True)
class Compiled:
    sql: str
    params: tuple[Any, ...]
    # Labels for the output columns whose meaning a bare key does not carry:
    # rolling and cumulative means, and every aggregated figure. A plain game
    # stat keeps the display tools' usual header.
    labels: dict[str, str]
    # "games" or "seasons": which view the statement reads
    source: str


def _rounded(metric: Metric, expr: str, *, averaged: bool) -> str:
    if metric.kind == "rate":
        return f"ROUND(({expr})::numeric, {RATE_PLACES})"
    if averaged:
        return f"ROUND(({expr})::numeric, {MEAN_PLACES})"
    return expr


def _agg_label(metric: Metric, agg: str) -> str:
    base = metric_label(metric.name)
    if metric.kind != "count":
        return base if agg != "max" else f"{base}, best game"
    return {"per_game": f"{base} per game", "total": f"{base}, total", "max": f"{base}, best game",
            "count_games": f"{base} per game"}[agg]


def _game_filters(spec: StatsSpec, seasons: Optional[list[str]], today: date) -> tuple[list[str], list[Any]]:
    conds: list[str] = ["min >= %s"]
    params: list[Any] = [spec.where.min_minutes if spec.where else 1]
    if spec.players:
        conds.append("player_id = ANY(%s)")
        params.append(list(spec.players))
    if seasons:
        conds.append("season = ANY(%s)")
        params.append(list(seasons))
    if spec.season_type != "all":
        conds.append("season_type = %s")
        params.append(spec.season_type)
    w = spec.window
    if w is not None:
        if w.last_days is not None:
            conds.append("date >= %s")
            params.append(today - timedelta(days=w.last_days))
        if w.from_date is not None:
            conds.append("date >= %s")
            params.append(w.from_date)
        if w.to_date is not None:
            conds.append("date <= %s")
            params.append(w.to_date)
    if spec.where is not None:
        for f in spec.where.stats:
            conds.append(f"{METRICS[f.metric].game_sql()} {_SQL_OPS[f.op]} %s")
            params.append(f.value)
        if spec.where.opponent is not None:
            conds.append("opponent = %s")
            params.append(spec.where.opponent)
        if spec.where.home is not None:
            conds.append("home = %s")
            params.append(spec.where.home)
    return conds, params


def _last_games(spec: StatsSpec, inner: str, columns: list[str], params: list[Any]) -> str:
    """Wrap a game-row selection to keep each player's last N games of it."""
    n = spec.window.last_games if spec.window else None
    if n is None:
        return inner
    params.append(n)
    return (f"SELECT {', '.join(columns)} FROM (SELECT g.*, ROW_NUMBER() OVER (PARTITION BY player_id "
            f"ORDER BY date DESC, game_no DESC) AS rn FROM ({inner}) g) w WHERE rn <= %s")


def _compile_game(spec: StatsSpec, seasons: Optional[list[str]], today: date) -> Compiled:
    conds, params = _game_filters(spec, seasons, today)
    selects = list(GAME_ROW_COLUMNS)
    columns = list(GAME_ROW_COLUMNS)
    labels: dict[str, str] = {}
    for name in spec.metrics:
        m = METRICS[name]
        selects.append(f"{_rounded(m, m.game_sql(), averaged=False)} AS {name}")
        columns.append(name)
        if spec.transform is not None:
            k = spec.transform.rolling_mean
            preceding = f"{k - 1} PRECEDING" if k else "UNBOUNDED PRECEDING"
            frame = (f"OVER (PARTITION BY player_id, season, season_type ORDER BY date, game_no "
                     f"ROWS BETWEEN {preceding} AND CURRENT ROW)")
            expr = _rounded(m, m.frame_sql(frame), averaged=True)
            if k:
                expr = f"CASE WHEN COUNT(*) {frame} >= {k} THEN {expr} END"
            col = f"{name}{spec.transform.suffix}"
            selects.append(f"{expr} AS {col}")
            columns.append(col)
            labels[col] = metric_label(name, rolling=k, cumulative=not k)
    inner = f"SELECT {', '.join(selects)} FROM {GAMES_VIEW} WHERE {' AND '.join(conds)}"
    body = _last_games(spec, inner, columns, params)
    if spec.rank is not None:
        order = f"{spec.rank.by} {spec.rank.order.upper()} NULLS LAST, date"
        limit = spec.rank.top
    else:
        order = "player_id, season, season_type, date"
        limit = ROW_CAP + 1
    params.append(limit)
    sql = f"SELECT {', '.join(columns)} FROM ({body}) q ORDER BY {order} LIMIT %s"
    return Compiled(sql, tuple(params), labels, "games")


def _compile_grouped(spec: StatsSpec, seasons: Optional[list[str]], today: date, *, by_season: bool) -> Compiled:
    """Total grain, and season grain from game rows: one row per player (and season)."""
    conds, params = _game_filters(spec, seasons, today)
    group = ["player_id", "name", *(["season"] if by_season else [])]
    needed = {"game_no", "date", *group}
    for name in spec.metrics:
        if name != "gp":  # COUNT(*) below, not a view column
            needed.update(METRICS[name].columns())
    inner = f"SELECT {', '.join(sorted(needed))} FROM {GAMES_VIEW} WHERE {' AND '.join(conds)}"
    rows_sql = _last_games(spec, inner, sorted(needed), params)
    agg = "per_game" if spec.agg == "count_games" else spec.agg
    selects = [*group, "COUNT(*) AS gp"]
    if not by_season:
        selects += ["MIN(date) AS first_date", "MAX(date) AS last_date"]
    labels: dict[str, str] = {"gp": "games" if spec.agg == "count_games" else "games played"}
    for name in spec.metrics:
        if name == "gp":
            continue
        m = METRICS[name]
        selects.append(f"{_rounded(m, m.agg_sql(agg), averaged=agg == 'per_game')} AS {name}")
        labels[name] = _agg_label(m, spec.agg)
    sql = f"SELECT {', '.join(selects)} FROM ({rows_sql}) t GROUP BY {', '.join(group)}"
    if spec.leaders:
        sql += " HAVING COUNT(*) >= %s"
        params.append(spec.effective_min_games())
    if spec.rank is not None:
        sql += f" ORDER BY {spec.rank.by} {spec.rank.order.upper()} NULLS LAST, {', '.join(group)}"
        params.append(spec.rank.top)
    else:
        sql += f" ORDER BY {', '.join(group)}"
        params.append(ROW_CAP + 1)
    sql += " LIMIT %s"
    return Compiled(sql, tuple(params), labels, "games")


def _compile_seasons_view(spec: StatsSpec, seasons: Optional[list[str]]) -> Compiled:
    conds: list[str] = []
    params: list[Any] = []
    if spec.players:
        conds.append("player_id = ANY(%s)")
        params.append(list(spec.players))
    if seasons:
        conds.append("season = ANY(%s)")
        params.append(list(seasons))
    if spec.leaders:
        conds.append("gp >= %s")
        params.append(spec.effective_min_games())
    selects = ["player_id", "name", "season", "gp"]
    labels: dict[str, str] = {"gp": "games played"}
    for name in spec.metrics:
        if name == "gp":
            continue
        m = METRICS[name]
        averaged = spec.agg == "per_game" and m.kind == "count"
        selects.append(f"{_rounded(m, m.season_sql(spec.agg), averaged=averaged)} AS {name}")
        labels[name] = _agg_label(m, spec.agg)
    sql = f"SELECT {', '.join(selects)} FROM {SEASONS_VIEW}"
    if conds:
        sql += f" WHERE {' AND '.join(conds)}"
    if spec.rank is not None:
        sql += f" ORDER BY {spec.rank.by} {spec.rank.order.upper()} NULLS LAST, player_id, season"
        params.append(spec.rank.top)
    else:
        sql += " ORDER BY player_id, season"
        params.append(ROW_CAP + 1)
    sql += " LIMIT %s"
    return Compiled(sql, tuple(params), labels, "seasons")


def compile_spec(spec: StatsSpec, *, seasons: Optional[list[str]], today: date) -> Compiled:
    """The statement for a validated spec. `seasons` is the resolved list (the
    caller fills the default in), or None for every season; `today` anchors
    `last_days`."""
    if spec.grain == "game":
        return _compile_game(spec, seasons, today)
    if spec.grain == "total":
        return _compile_grouped(spec, seasons, today, by_season=False)
    if spec._from_game_rows():
        return _compile_grouped(spec, seasons, today, by_season=True)
    return _compile_seasons_view(spec, seasons)
