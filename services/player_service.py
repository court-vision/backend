import unicodedata
from typing import Optional
from datetime import date, timedelta
from schemas.player import PlayerStatsResp, PlayerStats, AvgStats, GameLog
from schemas.common import ApiStatus
from db.models.nba.players import Player
from db.models.nba.player_game_stats import PlayerGameStats
from core.logging import get_logger


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
        log = get_logger()
        try:
            # Step 1: Resolve player from the Player dimension table
            player = None
            if espn_id is not None:
                player = Player.get_or_none(Player.espn_id == espn_id)
            elif player_id is not None:
                player = Player.get_or_none(Player.id == player_id)
            elif name is not None:
                normalized_name = _normalize_name(name)
                player = Player.get_or_none(Player.name_normalized == normalized_name)
            else:
                return PlayerStatsResp(
                    status=ApiStatus.ERROR,
                    message="Must provide either player_id or name",
                    data=None
                )

            if not player:
                return PlayerStatsResp(
                    status=ApiStatus.ERROR,
                    message="Player not found",
                    data=None
                )

            # Step 2: Get all game logs for the player from PlayerGameStats
            game_logs_query = (
                PlayerGameStats.select()
                .where(PlayerGameStats.player_id == player.id)
                .order_by(PlayerGameStats.game_date.asc())
            )

            # If team filter is provided, apply it
            if team is not None:
                game_logs_query = game_logs_query.where(PlayerGameStats.team_id == team)

            game_logs_list = list(game_logs_query)

            if not game_logs_list:
                return PlayerStatsResp(
                    status=ApiStatus.ERROR,
                    message="No game stats found for player",
                    data=None
                )

            # Get player info from Player dimension and most recent game
            latest_game = game_logs_list[-1]
            player_name = player.name
            player_team = latest_game.team_id
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
                    date=str(g.game_date),
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

            # Use the player ID from the Player dimension
            resolved_player_id = player.id

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
            log.error("get_player_stats_error", error=str(e), espn_id=espn_id, player_id=player_id, name=name)
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
            player_id: The player's NBA ID.
            days: Number of days to look back (default 7).

        Returns:
            Average fantasy points per game, or None if no games found.
        """
        cutoff_date = date.today() - timedelta(days=days)

        query = PlayerGameStats.select().where(
            (PlayerGameStats.player_id == player_id) &
            (PlayerGameStats.game_date >= cutoff_date)
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

        # Query all games for all players with JOIN to Player for espn_id
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.espn_id)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where(
                (Player.espn_id.in_(espn_ids)) &
                (PlayerGameStats.game_date >= cutoff_date)
            )
        )

        # Group games by ESPN ID
        player_games: dict[int, list] = {eid: [] for eid in espn_ids}
        for game in query:
            espn_id = game.player.espn_id
            if espn_id in player_games:
                player_games[espn_id].append(game)

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

    @staticmethod
    def get_last_n_day_avg_batch_by_name(
        players: list[tuple[str, str]],
        days: int = 7
    ) -> dict[str, float | None]:
        """
        Get last N day averages for multiple players by name (for Yahoo).

        Args:
            players: List of (name, team) tuples.
            days: Number of days to look back (default 7).

        Returns:
            Dict mapping normalized_name to their average fantasy points (or None).
        """
        if not players:
            return {}

        cutoff_date = date.today() - timedelta(days=days)

        # Build normalized name to team mapping
        name_team_map = {}
        normalized_names = []
        for name, team in players:
            normalized = _normalize_name(name)
            name_team_map[normalized] = team
            normalized_names.append(normalized)

        # Query all games with JOIN to Player for name_normalized
        query = (
            PlayerGameStats.select(PlayerGameStats, Player.name_normalized)
            .join(Player, on=(PlayerGameStats.player_id == Player.id))
            .where(
                (Player.name_normalized.in_(normalized_names)) &
                (PlayerGameStats.game_date >= cutoff_date)
            )
        )

        # Group games by normalized name (filtering by team for accuracy)
        player_games: dict[str, list] = {name: [] for name in normalized_names}
        for game in query:
            game_name_norm = game.player.name_normalized
            expected_team = name_team_map.get(game_name_norm)
            # Only include if team matches (handles players with same name)
            # team_id is the team abbreviation (FK string to NBATeam)
            if expected_team and game.team_id == expected_team:
                if game_name_norm in player_games:
                    player_games[game_name_norm].append(game)

        # Calculate averages
        result = {}
        for name in normalized_names:
            games = player_games[name]
            if games:
                total_fpts = sum(g.fpts for g in games)
                result[name] = round(total_fpts / len(games), 1)
            else:
                result[name] = None

        return result

