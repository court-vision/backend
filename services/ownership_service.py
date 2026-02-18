"""
Service for ownership trend operations.
"""

from datetime import date, timedelta

from core.logging import get_logger
from db.models.nba.players import Player
from db.models.nba.player_ownership import PlayerOwnership
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.player_game_stats import PlayerGameStats
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
        min_change: float = 3.0,
        min_ownership: float = 3.0,
        sort_by: str = "velocity",
        direction: str = "both",
        limit: int = 20,
    ) -> OwnershipTrendingResp:
        """
        Get players with trending ownership using velocity-based ranking.

        Velocity measures relative change: (current - past) / past * 100
        This surfaces breakout players better than absolute change alone.
        Example: 5% -> 15% = +200% velocity (breakout) vs 60% -> 65% = +8% (meh)

        Args:
            days: Lookback period in days
            min_change: Minimum ownership change in percentage points
            min_ownership: Minimum current ownership % to filter noise (default 3%)
            sort_by: 'velocity' (relative change) or 'change' (absolute change)
            direction: 'up', 'down', or 'both'
            limit: Maximum players per direction

        Returns:
            OwnershipTrendingResp with trending players sorted by velocity
        """
        log = get_logger()

        try:
            yesterday = date.today() - timedelta(days=1)
            past_date = yesterday - timedelta(days=days)

            # Get current and past ownership data
            current_data = {
                row.player_id: float(row.rost_pct)
                for row in PlayerOwnership.select().where(
                    PlayerOwnership.snapshot_date == yesterday
                )
            }
            # Find the closest available snapshot at or before past_date to handle
            # gaps where the pipeline didn't run on the exact target date
            actual_past_date = (
                PlayerOwnership.select(PlayerOwnership.snapshot_date)
                .where(PlayerOwnership.snapshot_date <= past_date)
                .order_by(PlayerOwnership.snapshot_date.desc())
                .limit(1)
                .scalar()
            )
            past_data = {}
            if actual_past_date:
                past_data = {
                    row.player_id: float(row.rost_pct)
                    for row in PlayerOwnership.select().where(
                        PlayerOwnership.snapshot_date == actual_past_date
                    )
                }

            # Calculate changes with velocity
            changes = []
            for player_id in current_data:
                current = current_data[player_id]
                past = past_data.get(player_id, 0)
                change = current - past

                # Apply minimum ownership filter to reduce noise from deep roster players
                if current < min_ownership and past < min_ownership:
                    continue

                # Calculate velocity (relative change as percentage)
                # For rising: use past as baseline (how much did they grow from baseline)
                # For falling: use current as baseline (how much did they shrink to)
                if change > 0:
                    # Rising: velocity = how much they grew relative to starting point
                    velocity = (change / past * 100) if past > 0 else 100.0
                else:
                    # Falling: velocity = how much they shrank relative to peak
                    velocity = (change / past * 100) if past > 0 else -100.0

                if abs(change) >= min_change:
                    changes.append({
                        "player_id": player_id,
                        "current": current,
                        "past": past,
                        "change": change,
                        "velocity": velocity,
                    })

            # Get player info for trending players
            player_ids = [c["player_id"] for c in changes]
            player_info = {
                p.id: p for p in Player.select().where(Player.id.in_(player_ids))
            }

            # Get team info from latest season stats, falling back to most recent game
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
                        if stats.team_id:
                            team_info[stats.player_id] = stats.team_id

                # Fill missing teams from most recent game stats
                missing_ids = [pid for pid in player_ids if pid not in team_info]
                if missing_ids:
                    for pid in missing_ids:
                        latest_game = (
                            PlayerGameStats.select(PlayerGameStats.team)
                            .where(
                                (PlayerGameStats.player_id == pid)
                                & (PlayerGameStats.team.is_null(False))
                            )
                            .order_by(PlayerGameStats.game_date.desc())
                            .first()
                        )
                        if latest_game and latest_game.team_id:
                            team_info[pid] = latest_game.team_id

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
                    velocity=round(c["velocity"], 1),
                )

                if c["change"] > 0:
                    trending_up.append(trending_player)
                else:
                    trending_down.append(trending_player)

            # Sort by velocity (default) or absolute change
            sort_key = "velocity" if sort_by == "velocity" else "change"
            trending_up.sort(key=lambda x: getattr(x, sort_key), reverse=True)
            trending_down.sort(key=lambda x: getattr(x, sort_key))

            trending_up = trending_up[:limit]
            trending_down = trending_down[:limit]

            # Filter by direction
            if direction == "up":
                trending_down = []
            elif direction == "down":
                trending_up = []

            return OwnershipTrendingResp(
                status=ApiStatus.SUCCESS,
                message=f"Trending players over {days} days (sorted by {sort_by})",
                data=OwnershipTrendingData(
                    days=days,
                    min_ownership=min_ownership,
                    sort_by=sort_by,
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
