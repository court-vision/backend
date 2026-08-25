"""
Per-player scalar values under a league's scoring.

One helper for every place that produces `avg_points`: rolling-window averages
(nba.player_rolling_stats), decay-weighted recent averages (nba.player_game_stats),
raw StatLines for category projections, and -- for H2H-category leagues -- the
fpts-scale category value (services.scoring.category_value). When the point
weights are the global default, values are read straight from the stored `fpts`
columns (exact parity with the pre-league behavior); otherwise they are
recomputed from raw stats.

`avg_points_for(scoring, ...)` is the single dispatcher every provider uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal, Mapping, Optional

from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.players import Player
from schemas.common import LeagueInfo
from services.scoring.category_rank import PoolRow
from services.scoring.category_value import category_values, rankable_categories
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

        cutoff = date.today() - timedelta(days=days)
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.espn_id)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.espn_id.in_(espn_ids)) & (PlayerGameStats.game_date >= cutoff))
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

        cutoff = date.today() - timedelta(days=days)
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.name_normalized)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.name_normalized.in_(names)) & (PlayerGameStats.game_date >= cutoff))
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
        today = date.today()
        cutoff = today - timedelta(days=days)
        rate = math.log(2) / half_life
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.espn_id)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.espn_id.in_(espn_ids)) & (PlayerGameStats.game_date >= cutoff))
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
        today = date.today()
        cutoff = today - timedelta(days=days)
        rate = math.log(2) / half_life
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.name_normalized)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where((Player.name_normalized.in_(names)) & (PlayerGameStats.game_date >= cutoff))
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
    def _category_pool(days: int) -> tuple[Optional[ValueSource], list[PoolRow]]:
        """The rolling pool for `days`, or last season's baseline pool when the
        current season has no fresh snapshot yet."""
        _, pool = load_pool(PlayerValueService._category_window(days))
        if pool:
            return "rolling", pool
        pool = load_baseline_pool()
        return ("baseline", pool) if pool else (None, [])

    @staticmethod
    def _category_scored(cat_defs: list[CategoryDef], days: int) -> tuple[Optional[ValueSource], list[PoolRow], dict[int, float]]:
        source, pool = PlayerValueService._category_pool(days)
        values = {pid: value for pid, (value, _z) in category_values(pool, cat_defs).items()} if pool else {}
        return source, pool, values

    @staticmethod
    def category_value_by_espn_id(espn_ids: list[int], cat_defs: list[CategoryDef],
                                  days: int = 14) -> dict[int, ValueResult]:
        """Category value for each ESPN id over the rolling window (last season's baseline before then)."""
        if not espn_ids:
            return {}
        source, pool, values = PlayerValueService._category_scored(cat_defs, days)
        by_espn = {row.espn_id: values[row.id] for row in pool if row.espn_id is not None and row.id in values}
        return {eid: (ValueResult(by_espn[eid], source) if eid in by_espn else ValueResult(None, None))
                for eid in espn_ids}

    @staticmethod
    def category_value_by_name(players: list[tuple[str, str]], cat_defs: list[CategoryDef],
                               days: int = 14) -> dict[str, ValueResult]:
        """Same, keyed by normalized name (Yahoo rosters); a name shared by two
        players resolves to the one on the expected team."""
        if not players:
            return {}
        source, pool, values = PlayerValueService._category_scored(cat_defs, days)
        by_name: dict[str, list[PoolRow]] = {}
        for row in pool:
            if row.id not in values:
                continue
            for key in {_normalize(row.name), row.name_normalized}:
                if key:
                    by_name.setdefault(key, []).append(row)

        out: dict[str, ValueResult] = {}
        for name, team in players:
            n = _normalize(name)
            candidates = by_name.get(n, [])
            match = next((r for r in candidates if team and r.team == team), None) or (candidates[0] if candidates else None)
            out[n] = ValueResult(values[match.id], source) if match is not None else ValueResult(None, None)
        return out

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
