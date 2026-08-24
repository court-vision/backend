"""
Per-player scalar values under a league's point weights.

One helper for every place that produces `avg_points`: rolling-window averages
(nba.player_rolling_stats), decay-weighted recent averages (nba.player_game_stats),
and raw StatLines for category projections. When the weights are the global
default, values are read straight from the stored `fpts` columns (exact parity
with the pre-league behavior); otherwise they are recomputed from raw stats.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Mapping, Optional

from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.players import Player
from schemas.common import LeagueInfo
from services.scoring.models import StatLine
from services.scoring.points import PointsScoring
from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

ROLLING_WINDOWS = (7, 14, 30)


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

    # ---- rolling-window averages (StatLines) --------------------------------

    @staticmethod
    def _latest_rolling_date(days: int):
        return (
            PlayerRollingStats.select(PlayerRollingStats.as_of_date)
            .where(PlayerRollingStats.window_days == days)
            .order_by(PlayerRollingStats.as_of_date.desc())
            .limit(1)
            .scalar()
        )

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
