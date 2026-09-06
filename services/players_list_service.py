"""
Service for player list operations.
"""

from peewee import fn

from core.logging import get_logger
from core.season import previous_season
from core.settings import settings
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.base import DB_RUNTIME_ERRORS, db_operation
from schemas.common import ApiStatus
from schemas.players_list import PlayersListResp, PlayersListData, PlayerListItem


class PlayersListService:
    """Service for listing and searching players."""

    @staticmethod
    def _league_ranks(player_ids: list[int]) -> dict[int, int]:
        """League-wide rank per player, from nba.rankings.

        `player_season_stats.rank` used to fill this field, but it was a rank
        among the players who happened to have a row written that night, ordered
        by cumulative season points -- not a rank in any league-wide sense. This
        reads the real one. Empty (so the field is null) before the season has
        any data.
        """
        if not player_ids:
            return {}
        from services.rankings_service import RankingsService

        ids = set(player_ids)
        return {
            row.id: int(row.curr_rank)
            for row in RankingsService._fetch_season_rows() if row.id in ids
        }

    @staticmethod
    def _latest_rows(season: str):
        """Season-stats rows joined to Player, one per player: the latest as_of_date within `season`."""
        return PlayerSeasonStats.latest_per_player(season)

    @staticmethod
    @db_operation("players.list")
    def list_players(
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

            as_of = query.select(fn.MAX(PlayerSeasonStats.as_of_date)).scalar()

            # Apply filters
            if team:
                query = query.where(PlayerSeasonStats.team_id == team.upper())

            if position:
                query = query.where(Player.position.contains(position.upper()))

            if min_games:
                query = query.where(PlayerSeasonStats.gp >= min_games)

            if name:
                name_normalized = name.strip().lower()
                query = query.where(fn.unaccent(Player.name_normalized).contains(fn.unaccent(name_normalized)))

            # Get total count before pagination
            total = query.count()

            # Apply pagination and ordering (cumulative fpts; the per-day `rank` is a cohort rank)
            query = (
                query.order_by(PlayerSeasonStats.fpts.desc(), Player.name.asc(), Player.id.asc())
                .offset(offset)
                .limit(limit)
            )

            rows = list(query)
            ranks = PlayersListService._league_ranks([row.player_id for row in rows])

            players = []
            for stats in rows:
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
                        rank=ranks.get(stats.player_id),
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
                    season=season,
                    as_of_date=as_of.isoformat() if as_of else None,
                ),
            )

        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            log.error("players_list_error", error=str(e))
            return PlayersListResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch players",
                data=None,
            )
