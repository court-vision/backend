import unicodedata
from typing import Optional
from datetime import date, timedelta
from schemas.player import PlayerStatsResp, PlayerStats, AvgStats, GameLog
from schemas.common import ApiStatus
from db.models.stats.daily_player_stats import DailyPlayerStats
from peewee import fn


def _normalize_name(name: str) -> str:
    """Normalize a name by removing diacritics and converting to lowercase."""
    normalized = unicodedata.normalize("NFD", name)
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return ascii_name.lower().strip()


class PlayerService:

    @staticmethod
    async def get_player_stats(
        espn_id: Optional[int] = None,
        player_id: Optional[int] = None,
        name: Optional[str] = None,
        team: Optional[str] = None
    ) -> PlayerStatsResp:
        try:
            # Build query based on provided parameters
            query = DailyPlayerStats.select()
            if espn_id is not None:
                query = query.where(DailyPlayerStats.espn_id == espn_id)
            elif player_id is not None:
                # Lookup by player ID (used for rankings)
                query = query.where(DailyPlayerStats.id == player_id)
            elif name is not None:
                # Lookup by normalized name (and optionally team) - used for public queries
                normalized_name = _normalize_name(name)
                query = query.where(DailyPlayerStats.name_normalized == normalized_name)
                if team is not None:
                    query = query.where(DailyPlayerStats.team == team)
            else:
                return PlayerStatsResp(
                    status=ApiStatus.ERROR,
                    message="Must provide either player_id or name",
                    data=None
                )
            
            # Get all game logs for the player, ordered by date
            game_logs_query = query.order_by(DailyPlayerStats.date.asc())

            game_logs_list = list(game_logs_query)

            if not game_logs_list:
                return PlayerStatsResp(
                    status=ApiStatus.ERROR,
                    message="Player not found",
                    data=None
                )

            # Get player info from the most recent game
            latest_game = game_logs_list[-1]
            player_name = latest_game.name
            player_team = latest_game.team
            games_played = len(game_logs_list)

            # Calculate averages
            total_fpts = sum(g.fpts for g in game_logs_list)
            total_pts = sum(g.pts for g in game_logs_list)
            total_reb = sum(g.reb for g in game_logs_list)
            total_ast = sum(g.ast for g in game_logs_list)
            total_stl = sum(g.stl for g in game_logs_list)
            total_blk = sum(g.blk for g in game_logs_list)
            total_tov = sum(g.tov for g in game_logs_list)
            total_min = sum(g.min for g in game_logs_list)

            # Calculate shooting totals for percentages
            total_fgm = sum(g.fgm for g in game_logs_list)
            total_fga = sum(g.fga for g in game_logs_list)
            total_fg3m = sum(g.fg3m for g in game_logs_list)
            total_fg3a = sum(g.fg3a for g in game_logs_list)
            total_ftm = sum(g.ftm for g in game_logs_list)
            total_fta = sum(g.fta for g in game_logs_list)

            # Calculate shooting percentages (handle division by zero)
            avg_fg_pct = round((total_fgm / total_fga) * 100, 1) if total_fga > 0 else 0.0
            avg_fg3_pct = round((total_fg3m / total_fg3a) * 100, 1) if total_fg3a > 0 else 0.0
            avg_ft_pct = round((total_ftm / total_fta) * 100, 1) if total_fta > 0 else 0.0

            avg_stats = AvgStats(
                avg_fpts=round(total_fpts / games_played, 1),
                avg_points=round(total_pts / games_played, 1),
                avg_rebounds=round(total_reb / games_played, 1),
                avg_assists=round(total_ast / games_played, 1),
                avg_steals=round(total_stl / games_played, 1),
                avg_blocks=round(total_blk / games_played, 1),
                avg_turnovers=round(total_tov / games_played, 1),
                avg_minutes=round(total_min / games_played, 1),
                avg_fg_pct=avg_fg_pct,
                avg_fg3_pct=avg_fg3_pct,
                avg_ft_pct=avg_ft_pct,
            )

            # Build game logs
            game_logs = [
                GameLog(
                    date=str(g.date),
                    fpts=g.fpts,
                    pts=g.pts,
                    reb=g.reb,
                    ast=g.ast,
                    stl=g.stl,
                    blk=g.blk,
                    tov=g.tov,
                    min=g.min,
                    fgm=g.fgm,
                    fga=g.fga,
                    fg3m=g.fg3m,
                    fg3a=g.fg3a,
                    ftm=g.ftm,
                    fta=g.fta,
                )
                for g in game_logs_list
            ]

            # Use the ID from the game log if we looked up by name
            resolved_player_id = player_id if player_id is not None else latest_game.id

            player_stats = PlayerStats(
                id=resolved_player_id,
                name=player_name,
                team=player_team,
                games_played=games_played,
                avg_stats=avg_stats,
                game_logs=game_logs,
            )

            return PlayerStatsResp(
                status=ApiStatus.SUCCESS,
                message="Player stats fetched successfully",
                data=player_stats
            )

        except Exception as e:
            print(f"Error in get_player_stats: {e}")
            return PlayerStatsResp(
                status=ApiStatus.ERROR,
                message="Internal server error",
                data=None
            )

    @staticmethod
    def get_last_n_day_avg(player_id: int, days: int = 7) -> Optional[float]:
        """
        Get average fantasy points over the last N days for a player.

        Args:
            player_id: The player's ID.
            days: Number of days to look back (default 7).

        Returns:
            Average fantasy points per game, or None if no games found.
        """
        cutoff_date = date.today() - timedelta(days=days)

        query = DailyPlayerStats.select().where(
            (DailyPlayerStats.id == player_id) &
            (DailyPlayerStats.date >= cutoff_date)
        )

        games = list(query)
        if not games:
            return None

        total_fpts = sum(g.fpts for g in games)
        return round(total_fpts / len(games), 1)

    @staticmethod
    def get_last_n_day_avg_batch(
        espn_ids: list[int],
        days: int = 7
    ) -> dict[int, Optional[float]]:
        """
        Get last N day averages for multiple players efficiently.

        Args:
            espn_ids: List of ESPN player IDs.
            days: Number of days to look back (default 7).

        Returns:
            Dict mapping espn_id to their average fantasy points (or None).
        """
        if not espn_ids:
            return {}

        cutoff_date = date.today() - timedelta(days=days)

        # Query all games for all players in one query using ESPN ID
        query = DailyPlayerStats.select().where(
            (DailyPlayerStats.espn_id.in_(espn_ids)) &
            (DailyPlayerStats.date >= cutoff_date)
        )

        # Group games by ESPN ID
        player_games: dict[int, list] = {eid: [] for eid in espn_ids}
        for game in query:
            if game.espn_id in player_games:
                player_games[game.espn_id].append(game)

        # Calculate averages
        result = {}
        for espn_id in espn_ids:
            games = player_games[espn_id]
            if games:
                total_fpts = sum(g.fpts for g in games)
                result[espn_id] = round(total_fpts / len(games), 1)
            else:
                result[espn_id] = None

        return result

