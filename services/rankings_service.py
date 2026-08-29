"""
Player rankings in two formats over the season or a rolling window.

- points:     stored fantasy points (nba.rankings materialized view /
              nba.player_rolling_stats).
- categories: sum of per-category z-scores over the eligible player pool
              (services.scoring.category_rank), for H2H category leagues.

Two entry points. `get_rankings` is the public one, scored by the platform
default. `get_league_rankings` scores the same pool by a team's league settings.
Both say which in `meta.scoring`: stored `fpts` is one hardcoded formula, and a
ranking that does not name what produced it is telling the reader it is
universal when it is not.

Every path is split the same way: one `run_db` call that materializes rows,
then one `run_cpu` call that turns them into the response. Building 582
pydantic objects and z-scoring a pool are pure CPU with no database work, and
holding a database permit for their duration is what made the category path
degrade 3.4x faster than the points path under concurrency
(docs/PRODUCTION_READINESS.md item 2).
"""

from datetime import date
from typing import TYPE_CHECKING, Optional

from db import base as db_base
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.stats.rankings import Rankings, RankingsSource
from core.compute import run_cpu
from core.logging import get_logger
from schemas.common import ApiStatus, CategoryDefResp
from schemas.rankings import RankingsMeta, RankingsPlayer, RankingsResp, RankingsScoring
from services.scoring.category_rank import RANKABLE_KEYS, PoolRow, compute_category_scores
from services.scoring.category_value import rankable_categories
from services.scoring.models import CategoryDef
from services.scoring.points import PointsScoring
from services.scoring.pool import load_pool
from services.scoring.vocab import DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS
from services import schedule_service

if TYPE_CHECKING:  # pragma: no cover
    from services.scoring.resolver import ResolvedScoring

VALID_WINDOWS = {7, 14, 30}
VALID_FORMATS = ("points", "categories")

# Games-played floor for category rankings, by window (None = full season).
# Deliberately 1 everywhere: the rankings page shows the season from day one,
# volatility included; `min_games` is an explicit opt-in filter.
DEFAULT_MIN_GAMES: dict[Optional[int], int] = {None: 1, 30: 1, 14: 1, 7: 1}

# Point weights that only a per-game log can produce: a season or rolling row
# has no notion of how many of those games were double-doubles.
GAME_ONLY_KEYS = ("dd", "td")

log = get_logger("rankings")


class RankingsService:

    @staticmethod
    async def get_rankings(
        window: Optional[int] = None,
        format: str = "points",
        categories: Optional[list[str]] = None,
        min_games: Optional[int] = None,
    ) -> RankingsResp:
        try:
            if window is not None and window not in VALID_WINDOWS:
                return RankingsService._bad_request(
                    f"Invalid window {window}; use one of {sorted(VALID_WINDOWS)} or omit for full season"
                )
            if format not in VALID_FORMATS:
                return RankingsService._bad_request(f"Invalid format '{format}'; use one of {list(VALID_FORMATS)}")

            basis = RankingsService._default_scoring(format)
            if format == "categories":
                cat_defs = RankingsService._category_defs(categories)
                return await RankingsService._get_category_rankings(window, cat_defs, min_games, basis)
            if window is not None:
                return await RankingsService._get_rolling_rankings(window, basis)
            return await RankingsService._get_season_rankings(basis)

        except ValueError as e:                     # an unrankable category key (see _category_defs)
            return RankingsService._bad_request(str(e))

    @staticmethod
    async def get_league_rankings(
        scoring: "ResolvedScoring",
        window: Optional[int] = None,
        min_games: Optional[int] = None,
    ) -> RankingsResp:
        """The same player pool, scored by one league's settings.

        Takes an already-resolved scoring (the `get_owned_team` dependency has
        loaded the league, so this needs no database access of its own) and
        dispatches on what that league actually scores.
        """
        try:
            if window is not None and window not in VALID_WINDOWS:
                return RankingsService._bad_request(
                    f"Invalid window {window}; use one of {sorted(VALID_WINDOWS)} or omit for full season"
                )

            basis = RankingsService._league_scoring(scoring)
            if scoring.is_categories:
                cat_defs = rankable_categories(scoring)
                return await RankingsService._get_category_rankings(window, cat_defs, min_games, basis)
            return await RankingsService._get_league_points_rankings(scoring, window, min_games, basis)

        except ValueError as e:
            return RankingsService._bad_request(str(e))

    # ---- points ---------------------------------------------------------------

    @staticmethod
    async def _get_season_rankings(basis: RankingsScoring) -> RankingsResp:
        """Full-season rankings: cumulative fpts, no GP floor."""
        rows = await db_base.run_db("rankings.season", RankingsService._fetch_season_rows)
        return await run_cpu("rankings.season.build", RankingsService._build_season_response, rows, basis)

    @staticmethod
    def _fetch_season_rows() -> list:
        """Season rows from the materialized nba.rankings, or its source view when that lags.

        The copy is refreshed by the post-game season-stats pipeline. If that
        refresh failed — or something wrote nba.player_season_stats outside the
        pipeline — the newest as_of_date in the copy no longer matches the
        table's, and we fall back to nba.rankings_source: the full query, which
        is what the endpoint used to run on every request. Slower, never stale.
        """
        rows = list(Rankings.select().order_by(Rankings.curr_rank))
        source_as_of = RankingsService._latest_season_date()
        copy_as_of = max((r.as_of_date for r in rows if r.as_of_date), default=None)

        if source_as_of is not None and copy_as_of != source_as_of:
            log.warning(
                "rankings_matview_stale",
                copy_as_of=str(copy_as_of),
                source_as_of=str(source_as_of),
                rows=len(rows),
            )
            return list(RankingsSource.select().order_by(RankingsSource.curr_rank))
        return rows

    @staticmethod
    def _build_season_response(rows: list, basis: RankingsScoring) -> RankingsResp:
        rankings_data = [
            RankingsPlayer(
                id=row.id,
                rank=row.curr_rank,
                player_name=row.name,
                team=row.team or "",           # as the rolling and category paths do: a
                total_fpts=float(row.fpts),    # traded player with no team is not a 400
                avg_fpts=float(row.avg_fpts),
                rank_change=row.rank_change,
                gp=int(row.gp) if row.gp is not None else None,
            )
            for row in rows
        ]
        # season / as_of / max_gp all ride on the rows themselves (migration
        # 0006); each used to be its own scan of nba.player_season_stats.
        season = next((row.season for row in rows if row.season), None) or RankingsService._current_season()
        as_of = max((row.as_of_date for row in rows if row.as_of_date), default=None)
        max_gp = max((int(row.gp) for row in rows if row.gp is not None), default=None)

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message="Rankings fetched successfully" if rankings_data else RankingsService._empty_message(season, "season"),
            data=rankings_data,
            meta=RankingsService._meta("points", None, as_of, [], len(rankings_data), season=season,
                                       max_gp=max_gp, scoring=basis),
        )

    @staticmethod
    async def _get_rolling_rankings(window: int, basis: RankingsScoring) -> RankingsResp:
        """Rankings from the rolling averages table for a given day window."""
        latest_date, records = await db_base.run_db(
            "rankings.rolling", PlayerRollingStats.get_latest_for_window, window
        )
        return await run_cpu(
            "rankings.rolling.build", RankingsService._build_rolling_response,
            window, latest_date, records, basis,
        )

    @staticmethod
    def _build_rolling_response(window: int, latest_date: Optional[date], records: list,
                                basis: RankingsScoring) -> RankingsResp:
        if not records:
            return RankingsResp(
                status=ApiStatus.SUCCESS,
                message=RankingsService._empty_message(RankingsService._current_season(), f"L{window}"),
                data=[],
                meta=RankingsService._meta("points", window, latest_date, [], 0, scoring=basis),
            )

        rankings_data = [
            RankingsPlayer(
                id=record.player_id,
                rank=rank,
                player_name=record.player.name,
                team=record.team_id or "",
                total_fpts=round(float(record.fpts) * record.gp, 1),
                avg_fpts=float(record.fpts),
                rank_change=0,
            )
            for rank, record in enumerate(records, start=1)
        ]
        for rp, record in zip(rankings_data, records):
            rp.gp = int(record.gp or 0)

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=f"L{window} rankings fetched successfully (as of {latest_date})",
            data=rankings_data,
            meta=RankingsService._meta("points", window, latest_date, [], len(rankings_data),
                                       max_gp=max((int(r.gp or 0) for r in records), default=None),
                                       scoring=basis),
        )

    # ---- categories -----------------------------------------------------------

    @staticmethod
    async def _get_category_rankings(
        window: Optional[int],
        cat_defs: list[CategoryDef],
        min_games: Optional[int],
        basis: RankingsScoring,
    ) -> RankingsResp:
        threshold = min_games if min_games is not None else DEFAULT_MIN_GAMES.get(window, 1)
        as_of, pool, season = await db_base.run_db(
            "rankings.pool", RankingsService._fetch_category_inputs, window
        )
        return await run_cpu(
            "rankings.categories.score", RankingsService._build_category_response,
            window, cat_defs, threshold, as_of, pool, season, basis,
        )

    @staticmethod
    def _fetch_category_inputs(window: Optional[int]) -> tuple[Optional[date], list[PoolRow], str]:
        """The pool for `window` plus the season it belongs to — every query the category path needs."""
        as_of, pool = RankingsService._load_pool(window)
        season = RankingsService._data_season() if window is None else RankingsService._current_season()
        return as_of, pool, season

    @staticmethod
    def _build_category_response(
        window: Optional[int],
        cat_defs: list[CategoryDef],
        threshold: int,
        as_of: Optional[date],
        pool: list[PoolRow],
        season: str,
        basis: RankingsScoring,
    ) -> RankingsResp:
        eligible = [row for row in pool if row.gp >= threshold]
        scored = compute_category_scores(eligible, cat_defs)

        rankings_data = [
            RankingsPlayer(
                id=s.row.id,
                rank=rank,
                player_name=s.row.name,
                team=s.row.team or "",
                total_fpts=s.row.fpts_total,
                avg_fpts=s.row.fpts_avg,
                rank_change=0,
                gp=s.row.gp,
                categories=s.values,
                category_z=s.z,
                score=s.score,
            )
            for rank, s in enumerate(scored, start=1)
        ]

        label = "Season" if window is None else f"L{window}"
        if rankings_data:
            message = f"{label} category rankings fetched successfully (as of {as_of})"
        elif pool and threshold > 1:
            message = f"No players with {threshold}+ games in the {label} window yet"
        else:
            message = RankingsService._empty_message(season, label)
        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=rankings_data,
            meta=RankingsService._meta("categories", window, as_of, cat_defs, len(eligible), threshold,
                                       season=season, max_gp=max((r.gp for r in pool), default=None),
                                       scoring=basis),
        )

    # ---- league points --------------------------------------------------------

    @staticmethod
    async def _get_league_points_rankings(
        scoring: "ResolvedScoring",
        window: Optional[int],
        min_games: Optional[int],
        basis: RankingsScoring,
    ) -> RankingsResp:
        """Points rankings under a league's own weights.

        Default weights take the stored paths unchanged: every `fpts` column in
        the database was computed with exactly those weights, so the
        materialized view and the rolling table already hold the right answer.
        Anything else has to be recomputed from the raw per-game stats.
        """
        if scoring.points.is_default:
            if window is None:
                return await RankingsService._get_season_rankings(basis)
            return await RankingsService._get_rolling_rankings(window, basis)

        threshold = min_games if min_games is not None else DEFAULT_MIN_GAMES.get(window, 1)
        as_of, pool, season = await db_base.run_db(
            "rankings.league_pool", RankingsService._fetch_category_inputs, window
        )
        return await run_cpu(
            "rankings.league.score", RankingsService._build_points_pool_response,
            window, scoring.points, threshold, as_of, pool, season, basis,
        )

    @staticmethod
    def _build_points_pool_response(
        window: Optional[int],
        points: PointsScoring,
        threshold: int,
        as_of: Optional[date],
        pool: list[PoolRow],
        season: str,
        basis: RankingsScoring,
    ) -> RankingsResp:
        """Rank a pool by a points formula applied to per-game stats.

        `PoolRow.line` is per-game, so the value is a per-game score and the
        total is that scaled by games played -- the same two numbers the stored
        paths report, under different weights.
        """
        eligible = [row for row in pool if row.gp >= threshold]
        scored = sorted(
            ((points.score(row.line), row) for row in eligible),
            key=lambda pair: (-pair[0], -pair[1].fpts_avg),
        )

        rankings_data = [
            RankingsPlayer(
                id=row.id,
                rank=rank,
                player_name=row.name,
                team=row.team or "",
                total_fpts=round(value * row.gp, 1),
                avg_fpts=round(value, 2),
                rank_change=0,
                gp=row.gp,
            )
            for rank, (value, row) in enumerate(scored, start=1)
        ]

        label = "Season" if window is None else f"L{window}"
        if rankings_data:
            message = f"{label} rankings fetched successfully (as of {as_of})"
        elif pool and threshold > 1:
            message = f"No players with {threshold}+ games in the {label} window yet"
        else:
            message = RankingsService._empty_message(season, label)
        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=rankings_data,
            meta=RankingsService._meta("points", window, as_of, [], len(eligible), threshold,
                                       season=season, max_gp=max((r.gp for r in pool), default=None),
                                       scoring=basis),
        )

    @staticmethod
    def _category_defs(keys: Optional[list[str]]) -> list[CategoryDef]:
        """Canonical CategoryDefs for the requested keys (standard 9-cat by default).

        Raises ValueError for keys that cannot be ranked from stored stats.
        """
        ordered: list[str] = []
        for key in (keys or DEFAULT_CATEGORIES):
            if key not in RANKABLE_KEYS:
                raise ValueError(f"Unknown category '{key}'. Allowed: {', '.join(RANKABLE_KEYS)}")
            if key not in ordered:
                ordered.append(key)
        return [CategoryDef.for_key(key) for key in ordered]

    @staticmethod
    def _load_pool(window: Optional[int]) -> tuple[Optional[date], list[PoolRow]]:
        """Per-game pool rows for a window (see services.scoring.pool.load_pool).

        Kept as a thin wrapper so callers and tests can stub the pool here.
        """
        return load_pool(window)

    # ---- what the ranking was scored by ---------------------------------------

    @staticmethod
    def _default_scoring(format: str) -> RankingsScoring:
        """The platform default: stored `fpts`, or z-scores over the requested categories."""
        if format == "categories":
            return RankingsScoring(basis="categories")
        return RankingsScoring(basis="default_points", point_weights=dict(DEFAULT_POINT_WEIGHTS))

    @staticmethod
    def _league_scoring(scoring: "ResolvedScoring") -> RankingsScoring:
        """The scoring block for a league-scored ranking.

        Reports the weights that were actually applied, which is why `dd`/`td`
        are stripped out and named in `unsupported` rather than listed as
        honored: they contribute nothing to a per-game average, and claiming
        otherwise would be the same kind of quiet inaccuracy this block exists
        to remove.
        """
        league = scoring.league
        if league is None:
            # No league row yet: this is the platform default, not anyone's settings.
            return RankingsService._default_scoring(scoring.format)
        if scoring.is_categories:
            return RankingsScoring(
                basis="categories",
                league_id=getattr(league, "id", None),
                league_name=getattr(league, "name", None),
                settings_synced=scoring.settings_synced,
            )
        weights = dict(scoring.points.weights)
        unsupported = [key for key in GAME_ONLY_KEYS if key in weights]
        for key in unsupported:
            weights.pop(key)
        return RankingsScoring(
            basis="league_points",
            point_weights=weights,
            league_id=getattr(league, "id", None),
            league_name=getattr(league, "name", None),
            settings_synced=scoring.settings_synced,
            unsupported=unsupported,
        )

    # ---- helpers --------------------------------------------------------------

    @staticmethod
    def _current_season() -> str:
        from core.settings import settings
        return settings.nba_season

    @staticmethod
    def _data_season() -> str:
        """Season of the newest season-stats row (what the season pool is scoped to); settings when unknown."""
        try:
            season = (
                PlayerSeasonStats.select(PlayerSeasonStats.season)
                .order_by(PlayerSeasonStats.as_of_date.desc())
                .limit(1)
                .scalar()
            )
        except Exception:
            season = None
        return season or RankingsService._current_season()

    @staticmethod
    def _empty_message(season: str, label: str) -> str:
        return f"No {season} {label} data yet — rankings start after opening night"

    @staticmethod
    def _latest_season_date() -> Optional[date]:
        return (
            PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
            .order_by(PlayerSeasonStats.as_of_date.desc())
            .limit(1)
            .scalar()
        )

    @staticmethod
    def _season_day(as_of: Optional[date]) -> Optional[int]:
        if as_of is None:
            return None
        try:
            return schedule_service.season_day(as_of)
        except Exception:
            return None

    @staticmethod
    def _meta(format: str, window: Optional[int], as_of: Optional[date], cat_defs: list[CategoryDef],
              pool_size: int, min_games: Optional[int] = None, season: Optional[str] = None,
              max_gp: Optional[int] = None, scoring: Optional[RankingsScoring] = None) -> RankingsMeta:
        return RankingsMeta(
            format=format,
            window=window,
            as_of=as_of,
            categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
            pool_size=pool_size,
            min_games=min_games,
            season=season or RankingsService._current_season(),
            season_day=RankingsService._season_day(as_of),
            max_gp=max_gp,
            scoring=scoring or RankingsService._default_scoring(format),
        )

    @staticmethod
    def _bad_request(message: str) -> RankingsResp:
        return RankingsResp(status=ApiStatus.BAD_REQUEST, message=message, data=[])
