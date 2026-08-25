"""
Service for player list operations.
"""

from peewee import fn

from core.logging import get_logger
from core.season import previous_season
from core.settings import settings
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from schemas.common import ApiStatus
from schemas.players_list import PlayersListResp, PlayersListData, PlayerListItem


class PlayersListService:
    """Service for listing and searching players."""

    @staticmethod
    def _latest_rows(season: str):
        """Season-stats rows joined to Player, one per player: the latest as_of_date within `season`."""
        latest = (
            PlayerSeasonStats.select(
                PlayerSeasonStats.player.alias("pid"),
                fn.MAX(PlayerSeasonStats.as_of_date).alias("max_date"),
            )
            .where(PlayerSeasonStats.season == season)
            .group_by(PlayerSeasonStats.player)
        ).alias("latest")
        return (
            PlayerSeasonStats.select(
                PlayerSeasonStats,
                Player.id,
                Player.espn_id,
                Player.name,
                Player.position,
            )
            .join(Player, on=(PlayerSeasonStats.player == Player.id))
            .switch(PlayerSeasonStats)
            .join(latest, on=((PlayerSeasonStats.player == latest.c.pid)
                              & (PlayerSeasonStats.as_of_date == latest.c.max_date)))
        )

    @staticmethod
    async def list_players(
        team: str | None = None,
        position: str | None = None,
        min_games: int | None = None,
        name: str | None = None,
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

            # Each player's latest row in the active season (rows are written only on
            # days a player's GP changes, so a single as_of_date would miss most of
            # the league). Before opening night, fall back to last season.
            season = settings.nba_season
            query = PlayersListService._latest_rows(season)
            note = ""
            if query.count() == 0:
                season = previous_season(settings.nba_season)
                query = PlayersListService._latest_rows(season)
                note = f" (no {settings.nba_season} data yet; showing {season})"
            if query.count() == 0:
                return PlayersListResp(
                    status=ApiStatus.SUCCESS,
                    message="No player data available",
                    data=PlayersListData(players=[], total=0, limit=limit, offset=offset),
                )

            # Apply filters
            if team:
                query = query.where(PlayerSeasonStats.team_id == team.upper())

            if position:
                query = query.where(Player.position.contains(position.upper()))

            if min_games:
                query = query.where(PlayerSeasonStats.gp >= min_games)

            if name:
                name_normalized = name.lower().strip()
                query = query.where(Player.name_normalized.contains(name_normalized))

            # Get total count before pagination
            total = query.count()

            # Apply pagination and ordering (cumulative fpts; the per-day `rank` is a cohort rank)
            query = (
                query.order_by(PlayerSeasonStats.fpts.desc(), Player.name.asc())
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
                message=f"Found {total} players{note}",
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
