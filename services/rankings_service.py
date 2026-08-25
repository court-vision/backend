"""
Player rankings in two formats over the season or a rolling window.

- points:     stored fantasy points (nba.rankings view / nba.player_rolling_stats).
- categories: sum of per-category z-scores over the eligible player pool
              (services.scoring.category_rank), for H2H category leagues.
"""

from datetime import date
from typing import Optional

from core.logging import get_logger
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.stats.rankings import Rankings
from schemas.common import ApiStatus, CategoryDefResp
from schemas.rankings import RankingsMeta, RankingsPlayer, RankingsResp
from services.scoring.category_rank import RANKABLE_KEYS, PoolRow, compute_category_scores
from services.scoring.models import CategoryDef, StatLine
from services.scoring.vocab import DEFAULT_CATEGORIES

VALID_WINDOWS = {7, 14, 30}
VALID_FORMATS = ("points", "categories")

# Games-played floor for category rankings, by window (None = full season).
# Z-scores over a pool are only meaningful once tiny samples are filtered out.
DEFAULT_MIN_GAMES: dict[Optional[int], int] = {None: 20, 30: 8, 14: 4, 7: 2}


class RankingsService:

    @staticmethod
    async def get_rankings(
        window: Optional[int] = None,
        format: str = "points",
        categories: Optional[list[str]] = None,
        min_games: Optional[int] = None,
    ) -> RankingsResp:
        log = get_logger()
        try:
            if window is not None and window not in VALID_WINDOWS:
                return RankingsService._bad_request(
                    f"Invalid window {window}; use one of {sorted(VALID_WINDOWS)} or omit for full season"
                )
            if format not in VALID_FORMATS:
                return RankingsService._bad_request(f"Invalid format '{format}'; use one of {list(VALID_FORMATS)}")

            if format == "categories":
                return RankingsService._get_category_rankings(window, categories, min_games)
            if window is not None:
                return RankingsService._get_rolling_rankings(window)
            return RankingsService._get_season_rankings()

        except ValueError as e:
            return RankingsService._bad_request(str(e))
        except Exception as e:
            log.error("get_rankings_error", error=str(e), window=window, format=format)
            return RankingsResp(status=ApiStatus.ERROR, message="Internal server error", data=[])

    # ---- points ---------------------------------------------------------------

    @staticmethod
    def _get_season_rankings() -> RankingsResp:
        """Return full-season rankings from the legacy Rankings model."""
        rankings_query = Rankings.select().order_by(Rankings.curr_rank)

        rankings_data = [
            RankingsPlayer(
                id=row.id,
                rank=row.curr_rank,
                player_name=row.name,
                team=row.team,
                total_fpts=float(row.fpts),
                avg_fpts=float(row.avg_fpts),
                rank_change=row.rank_change,
            )
            for row in rankings_query
        ]

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message="Rankings fetched successfully",
            data=rankings_data,
            meta=RankingsService._meta("points", None, RankingsService._latest_season_date(), [], len(rankings_data)),
        )

    @staticmethod
    def _get_rolling_rankings(window: int) -> RankingsResp:
        """Return rankings from the rolling averages table for a given day window."""
        latest_date, records = PlayerRollingStats.get_latest_for_window(window)

        if not records:
            return RankingsResp(
                status=ApiStatus.SUCCESS,
                message=f"No L{window} data available yet",
                data=[],
                meta=RankingsService._meta("points", window, latest_date, [], 0),
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

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=f"L{window} rankings fetched successfully (as of {latest_date})",
            data=rankings_data,
            meta=RankingsService._meta("points", window, latest_date, [], len(rankings_data)),
        )

    # ---- categories -----------------------------------------------------------

    @staticmethod
    def _get_category_rankings(
        window: Optional[int],
        categories: Optional[list[str]],
        min_games: Optional[int],
    ) -> RankingsResp:
        cat_defs = RankingsService._category_defs(categories)
        threshold = min_games if min_games is not None else DEFAULT_MIN_GAMES.get(window, 1)
        as_of, pool = RankingsService._load_pool(window)
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
        message = (
            f"{label} category rankings fetched successfully (as of {as_of})"
            if rankings_data else f"No {label} data available yet"
        )
        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=rankings_data,
            meta=RankingsService._meta("categories", window, as_of, cat_defs, len(eligible), threshold),
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
        """Per-game pool rows for a window: the latest rolling snapshot, or each
        player's latest season snapshot converted from totals to per-game."""
        if window is not None:
            latest_date, records = PlayerRollingStats.get_latest_for_window(window)
            pool: list[PoolRow] = []
            for rec in records:
                gp = int(rec.gp or 0)
                if gp < 1:
                    continue
                fpts_avg = float(rec.fpts)
                pool.append(PoolRow(
                    id=rec.player_id, name=rec.player.name, team=rec.team_id, gp=gp,
                    line=StatLine.from_row(rec, gp=1.0),
                    fpts_avg=fpts_avg, fpts_total=round(fpts_avg * gp, 1),
                ))
            return latest_date, pool

        # Season rows are only written on days a player's GP changes, so take each
        # player's latest row within the current season (as the nba.rankings view does)
        # rather than a single as_of_date.
        current_season = (
            PlayerSeasonStats.select(PlayerSeasonStats.season)
            .order_by(PlayerSeasonStats.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if not current_season:
            return None, []

        records = (
            PlayerSeasonStats.select(PlayerSeasonStats, Player)
            .join(Player)
            .where(PlayerSeasonStats.season == current_season)
            .distinct([PlayerSeasonStats.player])
            .order_by(PlayerSeasonStats.player, PlayerSeasonStats.as_of_date.desc())
        )

        as_of: Optional[date] = None
        pool = []
        for rec in records:
            gp = int(rec.gp or 0)
            if gp < 1:
                continue
            if as_of is None or rec.as_of_date > as_of:
                as_of = rec.as_of_date
            line = StatLine.from_row(rec).scaled(1 / gp)
            line.gp = 1.0
            fpts_total = float(rec.fpts)
            pool.append(PoolRow(
                id=rec.player_id, name=rec.player.name, team=rec.team_id, gp=gp, line=line,
                fpts_avg=round(fpts_total / gp, 2), fpts_total=fpts_total,
            ))
        return as_of, pool

    # ---- helpers --------------------------------------------------------------

    @staticmethod
    def _latest_season_date() -> Optional[date]:
        return (
            PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
            .order_by(PlayerSeasonStats.as_of_date.desc())
            .limit(1)
            .scalar()
        )

    @staticmethod
    def _meta(format: str, window: Optional[int], as_of: Optional[date], cat_defs: list[CategoryDef],
              pool_size: int, min_games: Optional[int] = None) -> RankingsMeta:
        return RankingsMeta(
            format=format,
            window=window,
            as_of=as_of,
            categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
            pool_size=pool_size,
            min_games=min_games,
        )

    @staticmethod
    def _bad_request(message: str) -> RankingsResp:
        return RankingsResp(status=ApiStatus.BAD_REQUEST, message=message, data=[])
