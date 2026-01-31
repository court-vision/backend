"""
Service for ownership trend operations.
"""

from datetime import date, timedelta

from core.logging import get_logger
from db.models.nba.players import Player
from db.models.nba.player_ownership import PlayerOwnership
from db.models.nba.player_season_stats import PlayerSeasonStats
from schemas.common import ApiStatus
from schemas.ownership import (
    OwnershipTrendingResp,
    OwnershipTrendingData,
    TrendingPlayer,
)


class OwnershipService:
    """Service for retrieving ownership trends."""

    @staticmethod
    async def get_trending(
        days: int = 7,
        min_change: float = 5.0,
        direction: str = "both",
        limit: int = 20,
    ) -> OwnershipTrendingResp:
        """
        Get players with trending ownership.

        Args:
            days: Lookback period in days
            min_change: Minimum ownership change percentage
            direction: 'up', 'down', or 'both'
            limit: Maximum players per direction

        Returns:
            OwnershipTrendingResp with trending players
        """
        log = get_logger()

        try:
            today = date.today()
            past_date = today - timedelta(days=days)

            # Get current and past ownership data
            current_data = {
                row.player_id: float(row.rost_pct)
                for row in PlayerOwnership.select().where(
                    PlayerOwnership.snapshot_date == today
                )
            }
            past_data = {
                row.player_id: float(row.rost_pct)
                for row in PlayerOwnership.select().where(
                    PlayerOwnership.snapshot_date == past_date
                )
            }

            # Calculate changes
            changes = []
            for player_id in current_data:
                current = current_data[player_id]
                past = past_data.get(player_id, 0)
                change = current - past
                if abs(change) >= min_change:
                    changes.append({
                        "player_id": player_id,
                        "current": current,
                        "past": past,
                        "change": change,
                    })

            # Get player info for trending players
            player_ids = [c["player_id"] for c in changes]
            player_info = {
                p.id: p for p in Player.select().where(Player.id.in_(player_ids))
            }

            # Get team info from latest season stats
            team_info = {}
            if player_ids:
                latest_date = (
                    PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
                    .order_by(PlayerSeasonStats.as_of_date.desc())
                    .limit(1)
                    .scalar()
                )
                if latest_date:
                    for stats in (
                        PlayerSeasonStats.select()
                        .where(
                            (PlayerSeasonStats.player_id.in_(player_ids))
                            & (PlayerSeasonStats.as_of_date == latest_date)
                        )
                    ):
                        team_info[stats.player_id] = stats.team_id

            # Build trending lists
            trending_up = []
            trending_down = []

            for c in changes:
                player = player_info.get(c["player_id"])
                if not player:
                    continue

                trending_player = TrendingPlayer(
                    player_id=c["player_id"],
                    player_name=player.name,
                    team=team_info.get(c["player_id"]),
                    current_ownership=round(c["current"], 1),
                    previous_ownership=round(c["past"], 1),
                    change=round(c["change"], 1),
                )

                if c["change"] > 0:
                    trending_up.append(trending_player)
                else:
                    trending_down.append(trending_player)

            # Sort and limit
            trending_up.sort(key=lambda x: x.change, reverse=True)
            trending_down.sort(key=lambda x: x.change)

            trending_up = trending_up[:limit]
            trending_down = trending_down[:limit]

            # Filter by direction
            if direction == "up":
                trending_down = []
            elif direction == "down":
                trending_up = []

            return OwnershipTrendingResp(
                status=ApiStatus.SUCCESS,
                message=f"Trending players over {days} days",
                data=OwnershipTrendingData(
                    days=days,
                    trending_up=trending_up,
                    trending_down=trending_down,
                ),
            )

        except Exception as e:
            log.error("ownership_trending_error", error=str(e))
            return OwnershipTrendingResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch trending players",
                data=None,
            )
