"""
Player Rolling Stats Pipeline

Materializes L7, L14, and L30 rolling averages from player_game_stats.
All three windows are stored in nba.player_rolling_stats with a window_days
discriminator column.

Stats are per-game averages. Shooting percentages are computed from
window totals (sum FGM / sum FGA) which is statistically correct.

Depends on: player_game_stats (must run first to have fresh game data)
"""

from datetime import timedelta

import pytz
from peewee import fn

from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_rolling_stats import PlayerRollingStats
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext

WINDOWS = [7, 14, 30]


class PlayerRollingStatsPipeline(BasePipeline):
    """
    Materialize L7/L14/L30 rolling averages for all players.

    For each window, queries player_game_stats for games within the
    calendar day range, aggregates totals, divides by games played to get
    per-game averages, and upserts to player_rolling_stats.
    """

    config = PipelineConfig(
        name="player_rolling_stats",
        display_name="Player Rolling Stats",
        description="Materializes L7/L14/L30 rolling per-game averages from player_game_stats",
        target_table="nba.player_rolling_stats",
        depends_on=("player_game_stats",),
    )

    def execute(self, ctx: PipelineContext) -> None:
        """Execute rolling stats materialization for all windows."""

        # Determine target date using the same CST 6am cutoff used elsewhere
        if ctx.date_override:
            target_date = ctx.date_override
        else:
            now_cst = ctx.started_at  # already in CST from PipelineContext
            if now_cst.hour < 6:
                target_date = (now_cst - timedelta(days=1)).date()
            else:
                target_date = now_cst.date()

        ctx.log.info("computing_rolling_stats", date=str(target_date), windows=WINDOWS)

        for window in WINDOWS:
            # Inclusive window: game on cutoff date counts
            cutoff = target_date - timedelta(days=window - 1)

            ctx.log.info("processing_window", window=window, cutoff=str(cutoff), end=str(target_date))

            # Aggregate stats for all players in this window
            agg_rows = list(
                PlayerGameStats
                .select(
                    PlayerGameStats.player,
                    fn.COUNT(PlayerGameStats.id).alias("gp"),
                    fn.SUM(PlayerGameStats.fpts).alias("total_fpts"),
                    fn.SUM(PlayerGameStats.pts).alias("total_pts"),
                    fn.SUM(PlayerGameStats.reb).alias("total_reb"),
                    fn.SUM(PlayerGameStats.ast).alias("total_ast"),
                    fn.SUM(PlayerGameStats.stl).alias("total_stl"),
                    fn.SUM(PlayerGameStats.blk).alias("total_blk"),
                    fn.SUM(PlayerGameStats.tov).alias("total_tov"),
                    fn.SUM(PlayerGameStats.min).alias("total_min"),
                    fn.SUM(PlayerGameStats.fgm).alias("total_fgm"),
                    fn.SUM(PlayerGameStats.fga).alias("total_fga"),
                    fn.SUM(PlayerGameStats.fg3m).alias("total_fg3m"),
                    fn.SUM(PlayerGameStats.fg3a).alias("total_fg3a"),
                    fn.SUM(PlayerGameStats.ftm).alias("total_ftm"),
                    fn.SUM(PlayerGameStats.fta).alias("total_fta"),
                )
                .where(PlayerGameStats.game_date.between(cutoff, target_date))
                .group_by(PlayerGameStats.player)
            )

            if not agg_rows:
                ctx.log.info("no_games_in_window", window=window)
                continue

            # Build player_id → team_id map using each player's most recent
            # game in the window. Query ordered desc so first occurrence per
            # player = most recent team.
            team_rows = list(
                PlayerGameStats
                .select(PlayerGameStats.player, PlayerGameStats.team, PlayerGameStats.game_date)
                .where(PlayerGameStats.game_date.between(cutoff, target_date))
                .order_by(PlayerGameStats.player, PlayerGameStats.game_date.desc())
            )
            team_map: dict[int, str | None] = {}
            for row in team_rows:
                pid = row.player_id
                if pid not in team_map:
                    team_map[pid] = row.team_id

            # Upsert one rolling stats record per player per window
            for row in agg_rows:
                pid = row.player_id
                gp = row.gp
                if not gp:
                    continue

                total_fgm = row.total_fgm or 0
                total_fga = row.total_fga or 0
                total_fg3m = row.total_fg3m or 0
                total_fg3a = row.total_fg3a or 0
                total_ftm = row.total_ftm or 0
                total_fta = row.total_fta or 0

                stats = {
                    "fpts": round((row.total_fpts or 0) / gp, 2),
                    "pts": round((row.total_pts or 0) / gp, 2),
                    "reb": round((row.total_reb or 0) / gp, 2),
                    "ast": round((row.total_ast or 0) / gp, 2),
                    "stl": round((row.total_stl or 0) / gp, 2),
                    "blk": round((row.total_blk or 0) / gp, 2),
                    "tov": round((row.total_tov or 0) / gp, 2),
                    "min": round((row.total_min or 0) / gp, 2),
                    "fgm": round(total_fgm / gp, 2),
                    "fga": round(total_fga / gp, 2),
                    "fg_pct": round(total_fgm / total_fga, 4) if total_fga > 0 else 0.0,
                    "fg3m": round(total_fg3m / gp, 2),
                    "fg3a": round(total_fg3a / gp, 2),
                    "fg3_pct": round(total_fg3m / total_fg3a, 4) if total_fg3a > 0 else 0.0,
                    "ftm": round(total_ftm / gp, 2),
                    "fta": round(total_fta / gp, 2),
                    "ft_pct": round(total_ftm / total_fta, 4) if total_fta > 0 else 0.0,
                }

                PlayerRollingStats.upsert_rolling_stats(
                    player_id=pid,
                    as_of_date=target_date,
                    window_days=window,
                    gp=gp,
                    stats=stats,
                    team_id=team_map.get(pid),
                    pipeline_run_id=ctx.run_id,
                )
                ctx.increment_records()

            ctx.log.info(
                "window_complete",
                window=window,
                player_count=len(agg_rows),
            )
