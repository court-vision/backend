"""
Per-player scalar values under a league's scoring.

One helper for every place that produces `avg_points`: rolling-window averages
(nba.player_rolling_stats), decay-weighted recent averages (nba.player_game_stats),
raw StatLines for category projections, and -- for H2H-category leagues -- the
fpts-scale category value (services.scoring.category_value). The season
projection (nba.player_projections) is valued on the same scales, as a prior for
players whose recent average rests on too few games. When the point
weights are the global default, values are read straight from the stored `fpts`
columns (exact parity with the pre-league behavior); otherwise they are
recomputed from raw stats.

`avg_points_for(scoring, ...)` is the single dispatcher every provider uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping, Optional

from peewee import fn

from core.nba_calendar import nba_date_et
from core.settings import settings
from core.sync_cache import SyncTTLCache
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.players import Player
from schemas.common import LeagueInfo
from services.scoring.category_rank import PoolRow
from services.scoring.category_value import category_values, category_values_in_pool, rankable_categories
from services.scoring.models import CategoryDef, StatLine
from services.scoring.points import PointsScoring
from services.scoring.pool import BASELINE_MIN_GP, baseline_records, baseline_season, load_baseline_pool, load_pool
from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

if TYPE_CHECKING:  # pragma: no cover
    from services.scoring.resolver import ResolvedScoring

ROLLING_WINDOWS = (7, 14, 30)

ValueSource = Literal["rolling", "recent", "baseline"]
ValueKind = Literal["fpts", "cat_value"]

__all__ = ["BASELINE_MIN_GP", "PlayerValueService", "ValueKind", "ValueResult", "ValueSource"]


@dataclass(frozen=True)
class ValueResult:
    """A per-player scalar and where it came from (None value = nothing known)."""
    value: Optional[float]
    source: Optional[ValueSource]

    @property
    def is_baseline(self) -> bool:
        return self.source == "baseline"


def _normalize(name: str) -> str:
    from services.player_service import _normalize_name
    return _normalize_name(name)


@dataclass(frozen=True, eq=False)
class _LoadedPool:
    """One window's category pool as loaded, shared through `_POOL_CACHE`, so
    read-only. Compared by identity: each load is its own version of the pool,
    and a scored pool is only served alongside the load it was scored from."""
    source: Optional[ValueSource]
    rows: tuple[PoolRow, ...]
    # False when the rolling snapshot may still have been mid-write: served to
    # the callers that asked, never kept (see ROLLING_SNAPSHOT_SETTLE).
    settled: bool = True


@dataclass(frozen=True)
class _ScoredPool:
    """One window's category pool z-scored over one league's categories: all a
    category valuation reads. It is shared by every request (and DB thread)
    through `_SCORED_CACHE`, so it is read-only — the mappings are proxies, the
    candidate lists tuples, and nothing mutates a PoolRow."""
    pool: _LoadedPool                               # the load these values were scored from
    source: Optional[ValueSource]
    values: Mapping[int, float]                     # player id -> category value
    by_espn: Mapping[int, float]                    # ESPN id -> category value
    by_name: Mapping[str, tuple[PoolRow, ...]]      # normalized name -> valued rows, in pool order


# How old a rolling snapshot's newest row must be before a load of it is kept.
# Prod writes each window's rows back to back in 8-14 s; a minute past the
# newest row the write is over (or has stalled, and the TTL bounds that too).
ROLLING_SNAPSHOT_SETTLE = timedelta(seconds=60)

# Category leagues value every player against the whole pool: ~506 rows loaded,
# hydrated and z-scored per valuation. A category Week open makes 10-13 of them
# (/lineup for each day shown, the matchup trio, /actions, streamers), and in
# prod each took ~50 ms alone but 265-416 ms inside that burst, holding one of
# the 16 DB permits throughout. The inputs change once a day (post-game writes
# the rolling snapshot), so each process keeps the loaded pool per window and
# the scored pool per (window, category set), and a valuation becomes a dict
# lookup.
#
# The key is the window and the configured season, not the snapshot's date: the
# rolling pipeline upserts row by row outside a transaction (8-14 s per window in
# prod, around 00:30 ET), so a date-keyed entry would rebuild on the first row
# of a new snapshot and then keep that partial pool for the whole TTL. The pool's
# TTL bounds how long a process lags a new snapshot, as it does for the rankings
# cache.
#
# Keying on the window alone does not remove the partial-pool risk, though: a
# load whose expiry falls inside the write (~3% a window, per process, per night)
# still reads only the rows written so far, and a category player not yet written
# has no value at all (the category path has no per-player baseline fallback). So a
# rolling load whose newest row is under ROLLING_SNAPSHOT_SETTLE old is served
# but not kept, and the next valuation loads again: within the write, the
# uncached behaviour. The real fix is data-platform writing each window in one
# transaction; until then this narrows the exposure to that write itself.
#
# A scored entry is also tied to the load it was scored from rather than
# to its own TTL alone: one scored late in a load's life would otherwise keep
# that load's values for most of another TTL after the pool moved on, and a
# league's board values (scored) and its projections (scored against the loaded
# pool directly) could come from different snapshots.
_POOL_CACHE = SyncTTLCache(
    "category_pool",
    ttl_seconds=settings.category_value_cache_ttl_seconds,
    max_entries=settings.category_value_cache_max_entries,
)
_SCORED_CACHE = SyncTTLCache(
    "category_values",
    ttl_seconds=settings.category_value_cache_ttl_seconds,
    max_entries=settings.category_value_cache_max_entries,
)


class PlayerValueService:

    # ---- weight resolution --------------------------------------------------

    @staticmethod
    def weights_for_team(team_id: Optional[int]) -> dict[str, float]:
        from services.scoring.resolver import resolve_scoring_for_team
        return resolve_scoring_for_team(team_id).point_weights

    @staticmethod
    def weights_for_league_info(league_info: LeagueInfo) -> dict[str, float]:
        from services.scoring.resolver import resolve_scoring_for_league_info
        return resolve_scoring_for_league_info(league_info).point_weights

    @staticmethod
    def scoring_for(league_info: Optional[LeagueInfo], team_id: Optional[int] = None) -> "ResolvedScoring":
        """The resolved scoring (format, weights, categories, preview) for a saved
        team when `team_id` is known, else for raw league credentials."""
        from services.scoring.resolver import resolve_scoring, resolve_scoring_for_league_info, resolve_scoring_for_team
        if team_id:
            return resolve_scoring_for_team(team_id)
        if league_info is not None:
            return resolve_scoring_for_league_info(league_info)
        return resolve_scoring(None)

    @staticmethod
    def value_kind_for(scoring: "ResolvedScoring") -> ValueKind:
        """What `avg_points` measures under this scoring."""
        return "cat_value" if scoring.is_categories else "fpts"

    # ---- rolling-window averages (StatLines) --------------------------------

    @staticmethod
    def _latest_rolling_date(days: int):
        """Latest *fresh* rolling snapshot (stale = last season → None)."""
        return PlayerRollingStats.latest_fresh_date(days)

    @staticmethod
    def rolling_lines_by_espn_id(espn_ids: list[int], days: int = 7) -> dict[int, Optional[StatLine]]:
        """Per-game average StatLine for each player (rolling window), keyed by ESPN id."""
        if not espn_ids:
            return {}
        result: dict[int, Optional[StatLine]] = {eid: None for eid in espn_ids}
        if days in ROLLING_WINDOWS:
            latest = PlayerValueService._latest_rolling_date(days)
            if not latest:
                return result
            records = (
                PlayerRollingStats.select(PlayerRollingStats, Player.espn_id)
                .join(Player)
                .where((Player.espn_id.in_(espn_ids))
                       & (PlayerRollingStats.window_days == days)
                       & (PlayerRollingStats.as_of_date == latest))
            )
            for rec in records:
                result[rec.player.espn_id] = StatLine.from_row(rec, gp=float(rec.gp or 0))
            return result

        cutoff = nba_date_et() - timedelta(days=days)
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.espn_id)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.espn_id.in_(espn_ids)) & (PlayerGameStats.game_date >= cutoff) & PlayerGameStats.for_fantasy())
        )
        games: dict[int, list[StatLine]] = {eid: [] for eid in espn_ids}
        for g in query:
            if g.player.espn_id in games:
                games[g.player.espn_id].append(StatLine.from_game_row(g))
        for eid, lines in games.items():
            if lines:
                result[eid] = StatLine.sum(lines).scaled(1 / len(lines))
        return result

    @staticmethod
    def rolling_lines_by_name(players: list[tuple[str, str]], days: int = 7) -> dict[str, Optional[StatLine]]:
        """Same as rolling_lines_by_espn_id, keyed by normalized name (Yahoo rosters)."""
        if not players:
            return {}
        name_team: dict[str, str] = {}
        names: list[str] = []
        for name, team in players:
            n = _normalize(name)
            name_team[n] = team
            names.append(n)
        result: dict[str, Optional[StatLine]] = {n: None for n in names}

        if days in ROLLING_WINDOWS:
            latest = PlayerValueService._latest_rolling_date(days)
            if not latest:
                return result
            records = (
                PlayerRollingStats.select(PlayerRollingStats, Player)
                .join(Player)
                .where((Player.name_normalized.in_(names))
                       & (PlayerRollingStats.window_days == days)
                       & (PlayerRollingStats.as_of_date == latest))
            )
            for rec in records:
                n = rec.player.name_normalized
                expected = name_team.get(n)
                if expected is None or rec.team_id == expected:
                    result[n] = StatLine.from_row(rec, gp=float(rec.gp or 0))
            return result

        cutoff = nba_date_et() - timedelta(days=days)
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.name_normalized)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.name_normalized.in_(names)) & (PlayerGameStats.game_date >= cutoff) & PlayerGameStats.for_fantasy())
        )
        games: dict[str, list[StatLine]] = {n: [] for n in names}
        for g in query:
            n = g.player.name_normalized
            expected = name_team.get(n)
            if expected and g.team_id == expected and n in games:
                games[n].append(StatLine.from_game_row(g))
        for n, lines in games.items():
            if lines:
                result[n] = StatLine.sum(lines).scaled(1 / len(lines))
        return result

    # ---- scalar values ------------------------------------------------------

    @staticmethod
    def _scorer(weights: Optional[Mapping[str, float]]) -> PointsScoring:
        return PointsScoring(weights if weights else DEFAULT_POINT_WEIGHTS)

    @staticmethod
    def rolling_avg_by_espn_id(espn_ids: list[int], days: int = 7,
                               weights: Optional[Mapping[str, float]] = None) -> dict[int, Optional[float]]:
        scorer = PlayerValueService._scorer(weights)
        if scorer.is_default and not scorer.uses_game_only_stats:
            from services.player_service import PlayerService
            return PlayerService._stored_last_n_day_avg_batch(espn_ids, days)
        lines = PlayerValueService.rolling_lines_by_espn_id(espn_ids, days)
        return {eid: (round(scorer.score(line), 1) if line else None) for eid, line in lines.items()}

    @staticmethod
    def rolling_avg_by_name(players: list[tuple[str, str]], days: int = 7,
                            weights: Optional[Mapping[str, float]] = None) -> dict[str, Optional[float]]:
        scorer = PlayerValueService._scorer(weights)
        if scorer.is_default and not scorer.uses_game_only_stats:
            from services.player_service import PlayerService
            return PlayerService._stored_last_n_day_avg_batch_by_name(players, days)
        lines = PlayerValueService.rolling_lines_by_name(players, days)
        return {n: (round(scorer.score(line), 1) if line else None) for n, line in lines.items()}

    @staticmethod
    def recent_weighted_avg_by_espn_id(espn_ids: list[int], days: int = 14, half_life: int = 7,
                                       weights: Optional[Mapping[str, float]] = None) -> dict[int, Optional[float]]:
        """Exponentially decay-weighted average of per-game values (a game `half_life` days ago counts 0.5x)."""
        if not espn_ids:
            return {}
        scorer = PlayerValueService._scorer(weights)
        today = nba_date_et()
        cutoff = today - timedelta(days=days)
        rate = math.log(2) / half_life
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.espn_id)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.espn_id.in_(espn_ids)) & (PlayerGameStats.game_date >= cutoff) & PlayerGameStats.for_fantasy())
        )
        acc: dict[int, list[tuple[float, float]]] = {eid: [] for eid in espn_ids}
        for g in query:
            eid = g.player.espn_id
            if eid in acc:
                value = float(g.fpts) if scorer.is_default else scorer.score(StatLine.from_game_row(g))
                acc[eid].append((value, math.exp(-rate * (today - g.game_date).days)))
        return {eid: PlayerValueService._weighted_mean(pairs) for eid, pairs in acc.items()}

    @staticmethod
    def recent_weighted_avg_by_name(players: list[tuple[str, str]], days: int = 14, half_life: int = 7,
                                    weights: Optional[Mapping[str, float]] = None) -> dict[str, Optional[float]]:
        if not players:
            return {}
        scorer = PlayerValueService._scorer(weights)
        name_team = {_normalize(n): t for n, t in players}
        names = list(name_team.keys())
        today = nba_date_et()
        cutoff = today - timedelta(days=days)
        rate = math.log(2) / half_life
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.name_normalized)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.name_normalized.in_(names)) & (PlayerGameStats.game_date >= cutoff) & PlayerGameStats.for_fantasy())
        )
        acc: dict[str, list[tuple[float, float]]] = {n: [] for n in names}
        for g in query:
            n = g.player.name_normalized
            expected = name_team.get(n)
            if n in acc and (expected is None or g.team_id == expected):
                value = float(g.fpts) if scorer.is_default else scorer.score(StatLine.from_game_row(g))
                acc[n].append((value, math.exp(-rate * (today - g.game_date).days)))
        return {n: PlayerValueService._weighted_mean(pairs) for n, pairs in acc.items()}

    @staticmethod
    def _weighted_mean(pairs: list[tuple[float, float]]) -> Optional[float]:
        if not pairs:
            return None
        total_w = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / total_w, 1) if total_w > 0 else None

    # ---- previous-season baseline ---------------------------------------------

    @staticmethod
    def _baseline_season(season: Optional[str]) -> str:
        return baseline_season(season)

    @staticmethod
    def _baseline_records(season: Optional[str], where):
        """Each player's final row of `season` (gp >= BASELINE_MIN_GP), joined to Player."""
        return baseline_records(season, where)

    @staticmethod
    def _per_game(rec) -> StatLine:
        gp = float(rec.gp or 0)
        line = StatLine.from_row(rec).scaled(1 / gp) if gp > 0 else StatLine.from_row(rec)
        line.gp = gp
        return line

    @staticmethod
    def baseline_lines_by_espn_id(espn_ids: list[int], season: Optional[str] = None) -> dict[int, Optional[StatLine]]:
        """Per-game StatLine from each player's previous-season totals, keyed by ESPN id."""
        if not espn_ids:
            return {}
        result: dict[int, Optional[StatLine]] = {eid: None for eid in espn_ids}
        for rec in PlayerValueService._baseline_records(season, Player.espn_id.in_(espn_ids)):
            result[rec.player.espn_id] = PlayerValueService._per_game(rec)
        return result

    @staticmethod
    def baseline_lines_by_name(players: list[tuple[str, str]], season: Optional[str] = None) -> dict[str, Optional[StatLine]]:
        """Same, keyed by normalized name (team is not matched — rosters change between seasons)."""
        if not players:
            return {}
        names = [_normalize(n) for n, _ in players]
        result: dict[str, Optional[StatLine]] = {n: None for n in names}
        for rec in PlayerValueService._baseline_records(season, Player.name_normalized.in_(names)):
            result[rec.player.name_normalized] = PlayerValueService._per_game(rec)
        return result

    # ---- composed values: current window → previous-season baseline ------------

    @staticmethod
    def _fill_baseline(primary: dict, primary_source: ValueSource, baseline_lines: dict,
                       scorer: PointsScoring) -> dict:
        out = {}
        for key, value in primary.items():
            if value is not None:
                out[key] = ValueResult(value, primary_source)
                continue
            line = baseline_lines.get(key)
            out[key] = ValueResult(round(scorer.score(line), 1), "baseline") if line else ValueResult(None, None)
        return out

    @staticmethod
    def value_by_espn_id(espn_ids: list[int], weights: Optional[Mapping[str, float]] = None,
                         days: int = 14) -> dict[int, ValueResult]:
        """Rolling-window average under `weights`, falling back to last season's per-game baseline."""
        if not espn_ids:
            return {}
        primary = PlayerValueService.rolling_avg_by_espn_id(espn_ids, days, weights)
        missing = [eid for eid, v in primary.items() if v is None]
        baseline = PlayerValueService.baseline_lines_by_espn_id(missing) if missing else {}
        return PlayerValueService._fill_baseline(primary, "rolling", baseline, PlayerValueService._scorer(weights))

    @staticmethod
    def value_by_name(players: list[tuple[str, str]], weights: Optional[Mapping[str, float]] = None,
                      days: int = 14) -> dict[str, ValueResult]:
        if not players:
            return {}
        primary = PlayerValueService.rolling_avg_by_name(players, days, weights)
        missing = [(n, t) for n, t in players if primary.get(_normalize(n)) is None]
        baseline = PlayerValueService.baseline_lines_by_name(missing) if missing else {}
        return PlayerValueService._fill_baseline(primary, "rolling", baseline, PlayerValueService._scorer(weights))

    @staticmethod
    def recent_value_by_espn_id(espn_ids: list[int], days: int = 14, half_life: int = 7,
                                weights: Optional[Mapping[str, float]] = None) -> dict[int, ValueResult]:
        """Decay-weighted recent average, falling back to last season's baseline."""
        if not espn_ids:
            return {}
        primary = PlayerValueService.recent_weighted_avg_by_espn_id(espn_ids, days, half_life, weights)
        missing = [eid for eid, v in primary.items() if v is None]
        baseline = PlayerValueService.baseline_lines_by_espn_id(missing) if missing else {}
        return PlayerValueService._fill_baseline(primary, "recent", baseline, PlayerValueService._scorer(weights))

    @staticmethod
    def recent_value_by_name(players: list[tuple[str, str]], days: int = 14, half_life: int = 7,
                             weights: Optional[Mapping[str, float]] = None) -> dict[str, ValueResult]:
        if not players:
            return {}
        primary = PlayerValueService.recent_weighted_avg_by_name(players, days, half_life, weights)
        missing = [(n, t) for n, t in players if primary.get(_normalize(n)) is None]
        baseline = PlayerValueService.baseline_lines_by_name(missing) if missing else {}
        return PlayerValueService._fill_baseline(primary, "recent", baseline, PlayerValueService._scorer(weights))

    # ---- category value (H2H-category leagues) ----------------------------------

    @staticmethod
    def _category_window(days: int) -> int:
        """Category values are z-scored over a whole rolling snapshot, so an
        arbitrary day count snaps to the nearest stored window."""
        return min(ROLLING_WINDOWS, key=lambda w: (abs(w - days), w))

    @staticmethod
    def _rolling_snapshot_written_at(window: int, as_of: date) -> Optional[datetime]:
        """When the newest row of `window`'s `as_of` snapshot was written (naive UTC,
        as data-platform stores it). One index scan of ~500 rows: 0.3 ms in prod."""
        return (
            PlayerRollingStats.select(fn.MAX(PlayerRollingStats.updated_at))
            .where((PlayerRollingStats.window_days == window) & (PlayerRollingStats.as_of_date == as_of))
            .scalar()
        )

    @staticmethod
    def _rolling_snapshot_settled(window: int, as_of: date) -> bool:
        """False while the snapshot may still be mid-write: its newest row is under
        ROLLING_SNAPSHOT_SETTLE old (or gone since the pool was read)."""
        written = PlayerValueService._rolling_snapshot_written_at(window, as_of)
        if written is None:
            return False
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - written >= ROLLING_SNAPSHOT_SETTLE

    @staticmethod
    def _load_category_pool(window: int) -> _LoadedPool:
        """The rolling pool for `window`, or last season's baseline pool when the
        current season has no fresh snapshot yet. Uncached: see `_category_pool`."""
        as_of, pool = load_pool(window)
        if pool:
            return _LoadedPool("rolling", tuple(pool),
                               settled=PlayerValueService._rolling_snapshot_settled(window, as_of))
        pool = load_baseline_pool()
        return _LoadedPool("baseline", tuple(pool)) if pool else _LoadedPool(None, ())

    @staticmethod
    def _cached_category_pool(window: int) -> _LoadedPool:
        return _POOL_CACHE.get_or_build(
            (window, settings.nba_season),
            lambda: PlayerValueService._load_category_pool(window),
            keep=lambda loaded: loaded.settled,
        )

    @staticmethod
    def _category_pool(days: int) -> tuple[Optional[ValueSource], list[PoolRow]]:
        """The rolling pool for `days`, or last season's baseline pool when the
        current season has no fresh snapshot yet.

        Served from the per-process cache (`_POOL_CACHE`); the list is the
        caller's own, but its rows are shared with every other caller and must
        not be mutated (use dataclasses.replace for a changed copy)."""
        loaded = PlayerValueService._cached_category_pool(PlayerValueService._category_window(days))
        return loaded.source, list(loaded.rows)

    @staticmethod
    def _score_category_pool(cat_defs: tuple[CategoryDef, ...], loaded: _LoadedPool) -> _ScoredPool:
        pool = loaded.rows
        values = {pid: value for pid, (value, _z) in category_values(list(pool), list(cat_defs)).items()} if pool else {}
        by_espn = {row.espn_id: values[row.id] for row in pool if row.espn_id is not None and row.id in values}
        by_name: dict[str, list[PoolRow]] = {}
        for row in pool:
            if row.id not in values:
                continue
            for key in {_normalize(row.name), row.name_normalized}:
                if key:
                    by_name.setdefault(key, []).append(row)
        return _ScoredPool(
            pool=loaded,
            source=loaded.source,
            values=MappingProxyType(values),
            by_espn=MappingProxyType(by_espn),
            by_name=MappingProxyType({key: tuple(rows) for key, rows in by_name.items()}),
        )

    @staticmethod
    def _category_scored(cat_defs: list[CategoryDef], days: int) -> _ScoredPool:
        """The pool for `days` z-scored over `cat_defs`, from the per-process cache.

        Keyed on what the scores depend on: the window, the season (the baseline
        pool is the one before it), and each category's key and direction in
        order — order included, because the z-sum adds categories in that order
        and floating-point addition is not associative. Labels are left out:
        the same categories named differently by two providers score the same.

        The current load comes first (a lookup on a hit), and a scored entry
        serves only while it was scored from that very load: once the pool is
        reloaded, the next valuation rescores it, so values never outlive the
        load they came from."""
        window = PlayerValueService._category_window(days)
        loaded = PlayerValueService._cached_category_pool(window)
        fingerprint = tuple((c.key, c.higher_is_better) for c in cat_defs)
        cats = tuple(cat_defs)
        return _SCORED_CACHE.get_or_build(
            (window, settings.nba_season, fingerprint),
            lambda: PlayerValueService._score_category_pool(cats, loaded),
            valid=lambda scored: scored.pool is loaded,
        )

    @staticmethod
    def clear_category_cache() -> None:
        """Drop every cached category pool and score (tests; a new snapshot otherwise waits out the TTL)."""
        _POOL_CACHE.clear()
        _SCORED_CACHE.clear()

    @staticmethod
    def category_value_by_espn_id(espn_ids: list[int], cat_defs: list[CategoryDef],
                                  days: int = 14) -> dict[int, ValueResult]:
        """Category value for each ESPN id over the rolling window (last season's baseline before then)."""
        if not espn_ids:
            return {}
        scored = PlayerValueService._category_scored(cat_defs, days)
        return {eid: (ValueResult(scored.by_espn[eid], scored.source) if eid in scored.by_espn else ValueResult(None, None))
                for eid in espn_ids}

    @staticmethod
    def category_value_by_name(players: list[tuple[str, str]], cat_defs: list[CategoryDef],
                               days: int = 14) -> dict[str, ValueResult]:
        """Same, keyed by normalized name (Yahoo rosters); a name shared by two
        players resolves to the one on the expected team."""
        if not players:
            return {}
        scored = PlayerValueService._category_scored(cat_defs, days)
        out: dict[str, ValueResult] = {}
        for name, team in players:
            n = _normalize(name)
            candidates = scored.by_name.get(n, ())
            match = next((r for r in candidates if team and r.team == team), None) or (candidates[0] if candidates else None)
            out[n] = ValueResult(scored.values[match.id], scored.source) if match is not None else ValueResult(None, None)
        return out

    # ---- sample sizes and the season projection (a prior for thin samples) ------

    @staticmethod
    def rolling_games_by_espn_id(espn_ids: list[int], days: int = 14) -> dict[int, int]:
        """Games behind each player's rolling-window value, keyed by ESPN id. A player
        with no row (no game in the window, or no fresh snapshot) is absent."""
        if days not in ROLLING_WINDOWS:
            raise ValueError(f"no stored rolling window of {days} days; have {ROLLING_WINDOWS}")
        if not espn_ids:
            return {}
        latest = PlayerValueService._latest_rolling_date(days)
        if not latest:
            return {}
        records = (
            PlayerRollingStats.select(PlayerRollingStats.gp, Player.espn_id)
            .join(Player)
            .where((Player.espn_id.in_(espn_ids))
                   & (PlayerRollingStats.window_days == days)
                   & (PlayerRollingStats.as_of_date == latest))
        )
        return {rec.player.espn_id: int(rec.gp or 0) for rec in records}

    @staticmethod
    def projections_by_espn_id(espn_ids: list[int], season: Optional[str] = None) -> dict[int, PlayerProjection]:
        """Each player's row in the season's latest projection snapshot, Player joined:
        Court Vision's once the cv-projection chain has published one, ESPN's until
        then. One snapshot, one source (as on the draft board) — a player CV does not
        project gets nothing rather than ESPN's line."""
        if not espn_ids:
            return {}
        from core.settings import settings

        season = season or settings.nba_season
        for source in ("cv", "espn"):
            in_source = (PlayerProjection.season == season) & (PlayerProjection.source == source)
            latest = PlayerProjection.select(fn.MAX(PlayerProjection.as_of_date)).where(in_source).scalar()
            if latest is None:
                continue
            records = (
                PlayerProjection.select(PlayerProjection, Player)
                .join(Player)
                .where(in_source & (PlayerProjection.as_of_date == latest) & (Player.espn_id.in_(espn_ids)))
            )
            return {rec.player.espn_id: rec for rec in records}
        return {}

    @staticmethod
    def projected_value_by_espn_id(scoring: "ResolvedScoring", espn_ids: list[int], *,
                                   season: Optional[str] = None, days: int = 14) -> dict[int, float]:
        """Each player's per-game value from the season projection, on the scale
        `avg_points_for` gives the same league: fantasy points under its weights, or
        the category value of the projected line z-scored against the pool the
        rolling values come from (`days` picks it, as there), the player's own row
        swapped for his projection. Players without a projection are absent."""
        records = PlayerValueService.projections_by_espn_id(espn_ids, season)
        if not records:
            return {}
        lines = {eid: StatLine.from_row(rec) for eid, rec in records.items()}
        if not scoring.is_categories:
            scorer = PlayerValueService._scorer(scoring.point_weights)
            return {eid: round(scorer.score(line), 1) for eid, line in lines.items()}

        # The current load, which `_category_scored` also scores the board's
        # values from, so a drop prior weighs projection and value like for like.
        _, pool = PlayerValueService._category_pool(days)
        if not pool:
            return {}
        default = PlayerValueService._scorer(None)
        rows = []
        for eid, rec in records.items():
            fpts = round(default.score(lines[eid]), 1)
            gp = max(int(rec.projected_gp or 0), 1)    # a projected line counts even with no games projected
            rows.append(PoolRow(
                id=rec.player_id, name=rec.player.name, team=None, gp=gp, line=lines[eid],
                fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
                espn_id=eid, name_normalized=rec.player.name_normalized,
            ))
        values = category_values_in_pool(pool, rows, rankable_categories(scoring))
        return {row.espn_id: values[row.id] for row in rows if row.id in values}

    # ---- the one dispatcher every provider uses --------------------------------

    @staticmethod
    def avg_points_for(scoring: "ResolvedScoring", *, espn_ids: Optional[list[int]] = None,
                       names: Optional[list[tuple[str, str]]] = None, days: int = 14,
                       recent: bool = False) -> dict:
        """`avg_points` for a set of players under a league's resolved scoring.

        Category leagues (including a `scoring_preview="categories"` team) get the
        fpts-scale category value over the league's rankable categories; points
        leagues get fantasy points under the league's weights (rolling window, or
        the decay-weighted recent average with `recent=True`). Both fall back to
        last season's per-game baseline. Keys are ESPN ids or normalized names,
        matching the `espn_ids` / `names` (name, team) input given.
        """
        if scoring.is_categories:
            cats = rankable_categories(scoring)
            # No decay-weighted category pool exists; "recent" means the shortest window.
            window = min(days, ROLLING_WINDOWS[0]) if recent else days
            if names is not None:
                return PlayerValueService.category_value_by_name(names, cats, days=window)
            return PlayerValueService.category_value_by_espn_id(espn_ids or [], cats, days=window)

        weights = scoring.point_weights
        if names is not None:
            if recent:
                return PlayerValueService.recent_value_by_name(names, days=days, weights=weights)
            return PlayerValueService.value_by_name(names, weights=weights, days=days)
        if recent:
            return PlayerValueService.recent_value_by_espn_id(espn_ids or [], days=days, weights=weights)
        return PlayerValueService.value_by_espn_id(espn_ids or [], weights=weights, days=days)
