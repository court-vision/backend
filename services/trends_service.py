"""
Service for player trend operations.
"""

from datetime import date, timedelta

from core.logging import get_logger
from db.models.nba.players import Player
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.player_ownership import PlayerOwnership
from schemas.common import ApiStatus
from schemas.player_trends import (
    PlayerTrendsResp,
    PlayerTrendsData,
    TrendPeriod,
    OwnershipTrend,
)
from peewee import fn


class TrendsService:
    """Service for retrieving player trends."""

    @staticmethod
    async def get_player_trends(player_id: int) -> PlayerTrendsResp:
        """
        Get trend data for a player.

        Args:
            player_id: NBA player ID

        Returns:
            PlayerTrendsResp with trend data
        """
        log = get_logger()

        try:
            # Get player info
            player = Player.get_or_none(Player.id == player_id)
            if not player:
                return PlayerTrendsResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Player with ID {player_id} not found",
                    data=None,
                )

            today = date.today()
            trends = {}

            # Calculate trends for different periods
            for period_name, days in [
                ("last_7_days", 7),
                ("last_14_days", 14),
                ("last_30_days", 30),
            ]:
                cutoff = today - timedelta(days=days)
                games = list(
                    PlayerGameStats.select()
                    .where(
                        (PlayerGameStats.player_id == player_id)
                        & (PlayerGameStats.game_date >= cutoff)
                    )
                    .order_by(PlayerGameStats.game_date.desc())
                )

                if games:
                    total_fpts = sum(g.fpts for g in games)
                    avg_fpts = total_fpts / len(games)
                    trends[period_name] = TrendPeriod(
                        avg_fpts=round(avg_fpts, 1),
                        games=len(games),
                    )

            # Get current rank
            latest_season_stats = (
                PlayerSeasonStats.select()
                .where(PlayerSeasonStats.player_id == player_id)
                .order_by(PlayerSeasonStats.as_of_date.desc())
                .first()
            )

            current_rank = latest_season_stats.rank if latest_season_stats else None
            current_team = latest_season_stats.team_id if latest_season_stats else None

            # Get ownership trend
            ownership_trend = None
            ownership_records = PlayerOwnership.get_player_trend(player_id, days=7)
            if ownership_records:
                current = float(ownership_records[-1].rost_pct)
                past = float(ownership_records[0].rost_pct) if len(ownership_records) > 1 else current
                ownership_trend = OwnershipTrend(
                    current=round(current, 1),
                    change_7d=round(current - past, 1),
                )

            return PlayerTrendsResp(
                status=ApiStatus.SUCCESS,
                message=f"Trends for {player.name}",
                data=PlayerTrendsData(
                    player_id=player_id,
                    player_name=player.name,
                    team=current_team,
                    current_rank=current_rank,
                    trends=trends,
                    ownership=ownership_trend,
                ),
            )

        except Exception as e:
            log.error("player_trends_error", error=str(e), player_id=player_id)
            return PlayerTrendsResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch player trends",
                data=None,
            )
