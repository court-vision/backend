"""
Service for player list operations.
"""

from core.logging import get_logger
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from schemas.common import ApiStatus
from schemas.players_list import PlayersListResp, PlayersListData, PlayerListItem


class PlayersListService:
    """Service for listing and searching players."""

    @staticmethod
    async def list_players(
        team: str | None = None,
        position: str | None = None,
        min_games: int | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PlayersListResp:
        """
        List players with optional filters.

        Args:
            team: Filter by team abbreviation
            position: Filter by position
            min_games: Minimum games played
            search: Search by player name
            limit: Maximum results (default 50, max 100)
            offset: Offset for pagination

        Returns:
            PlayersListResp with list of players
        """
        log = get_logger()

        try:
            # Clamp limit
            limit = min(max(1, limit), 100)

            # Get the most recent date with season stats
            latest_date = (
                PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
                .order_by(PlayerSeasonStats.as_of_date.desc())
                .limit(1)
                .scalar()
            )

            if not latest_date:
                return PlayersListResp(
                    status=ApiStatus.SUCCESS,
                    message="No player data available",
                    data=PlayersListData(players=[], total=0, limit=limit, offset=offset),
                )

            # Build query joining players with their latest season stats
            query = (
                PlayerSeasonStats.select(
                    PlayerSeasonStats,
                    Player.id,
                    Player.espn_id,
                    Player.name,
                    Player.position,
                )
                .join(Player, on=(PlayerSeasonStats.player == Player.id))
                .where(PlayerSeasonStats.as_of_date == latest_date)
            )

            # Apply filters
            if team:
                query = query.where(PlayerSeasonStats.team == team.upper())

            if position:
                query = query.where(Player.position.contains(position.upper()))

            if min_games:
                query = query.where(PlayerSeasonStats.gp >= min_games)

            if search:
                search_normalized = search.lower().strip()
                query = query.where(Player.name_normalized.contains(search_normalized))

            # Get total count before pagination
            total = query.count()

            # Apply pagination and ordering
            query = (
                query.order_by(PlayerSeasonStats.rank.asc(nulls="last"))
                .offset(offset)
                .limit(limit)
            )

            players = []
            for stats in query:
                avg_fpts = stats.fpts / stats.gp if stats.gp > 0 else 0.0
                players.append(
                    PlayerListItem(
                        id=stats.player.id,
                        espn_id=stats.player.espn_id,
                        name=stats.player.name,
                        team=stats.team_id,
                        position=stats.player.position,
                        games_played=stats.gp,
                        avg_fpts=round(avg_fpts, 1),
                        rank=stats.rank,
                    )
                )

            return PlayersListResp(
                status=ApiStatus.SUCCESS,
                message=f"Found {total} players",
                data=PlayersListData(
                    players=players,
                    total=total,
                    limit=limit,
                    offset=offset,
                ),
            )

        except Exception as e:
            log.error("players_list_error", error=str(e))
            return PlayersListResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch players",
                data=None,
            )
