import unicodedata
from typing import Optional
from datetime import date, timedelta
from peewee import fn, Case
from schemas.player import (
    PlayerStatsResp,
    PlayerStats,
    AvgStats,
    AdvancedStatsData,
    GameLog,
    PercentileData,
    PlayerPercentilesResp,
)
from schemas.common import ApiStatus
from db.models.nba.players import Player
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_advanced_stats import PlayerAdvancedStats
from core.logging import get_logger


def _parse_window_size(window: str) -> int | None:
    """Parse a window string into a game count. Returns None for full season."""
    if window == "season":
        return None
    if window.startswith("l") and window[1:].isdigit():
        return int(window[1:])
    return None


def _normalize_name(name: str) -> str:
    """Normalize a name by removing diacritics and converting to lowercase."""
    normalized = unicodedata.normalize("NFD", name)
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return ascii_name.lower().strip()


def _compute_avg_stats(game_logs_list: list) -> AvgStats:
    """Compute average stats from a list of game log records."""
    games = len(game_logs_list)
    if games == 0:
        return AvgStats(
            avg_fpts=0, avg_points=0, avg_rebounds=0, avg_assists=0,
            avg_steals=0, avg_blocks=0, avg_turnovers=0, avg_minutes=0,
            avg_fg_pct=0, avg_fg3_pct=0, avg_ft_pct=0,
            avg_ts_pct=0, avg_efg_pct=0, avg_three_rate=0, avg_ft_rate=0,
            avg_fgm=0, avg_fga=0, avg_fg3m=0, avg_fg3a=0, avg_ftm=0, avg_fta=0,
        )

    total_fpts = sum(g.fpts for g in game_logs_list)
    total_pts = sum(g.pts for g in game_logs_list)
    total_reb = sum(g.reb for g in game_logs_list)
    total_ast = sum(g.ast for g in game_logs_list)
    total_stl = sum(g.stl for g in game_logs_list)
    total_blk = sum(g.blk for g in game_logs_list)
    total_tov = sum(g.tov for g in game_logs_list)
    total_min = sum(g.min for g in game_logs_list)
    total_fgm = sum(g.fgm for g in game_logs_list)
    total_fga = sum(g.fga for g in game_logs_list)
    total_fg3m = sum(g.fg3m for g in game_logs_list)
    total_fg3a = sum(g.fg3a for g in game_logs_list)
    total_ftm = sum(g.ftm for g in game_logs_list)
    total_fta = sum(g.fta for g in game_logs_list)

    # Basic shooting percentages
    avg_fg_pct = round((total_fgm / total_fga) * 100, 1) if total_fga > 0 else 0.0
    avg_fg3_pct = round((total_fg3m / total_fg3a) * 100, 1) if total_fg3a > 0 else 0.0
    avg_ft_pct = round((total_ftm / total_fta) * 100, 1) if total_fta > 0 else 0.0

    # Advanced shooting efficiency
    # TS% = PTS / (2 * (FGA + 0.44 * FTA))
    tsa = total_fga + 0.44 * total_fta
    avg_ts_pct = round((total_pts / (2 * tsa)) * 100, 1) if tsa > 0 else 0.0

    # EFG% = (FGM + 0.5 * 3PM) / FGA
    avg_efg_pct = round(((total_fgm + 0.5 * total_fg3m) / total_fga) * 100, 1) if total_fga > 0 else 0.0

    # 3P Rate = 3PA / FGA
    avg_three_rate = round((total_fg3a / total_fga) * 100, 1) if total_fga > 0 else 0.0

    # FT Rate = FTA / FGA
    avg_ft_rate = round((total_fta / total_fga) * 100, 1) if total_fga > 0 else 0.0

    return AvgStats(
        avg_fpts=round(total_fpts / games, 1),
        avg_points=round(total_pts / games, 1),
        avg_rebounds=round(total_reb / games, 1),
        avg_assists=round(total_ast / games, 1),
        avg_steals=round(total_stl / games, 1),
        avg_blocks=round(total_blk / games, 1),
        avg_turnovers=round(total_tov / games, 1),
        avg_minutes=round(total_min / games, 1),
        avg_fg_pct=avg_fg_pct,
        avg_fg3_pct=avg_fg3_pct,
        avg_ft_pct=avg_ft_pct,
        avg_ts_pct=avg_ts_pct,
        avg_efg_pct=avg_efg_pct,
        avg_three_rate=avg_three_rate,
        avg_ft_rate=avg_ft_rate,
        avg_fgm=round(total_fgm / games, 1),
        avg_fga=round(total_fga / games, 1),
        avg_fg3m=round(total_fg3m / games, 1),
        avg_fg3a=round(total_fg3a / games, 1),
        avg_ftm=round(total_ftm / games, 1),
        avg_fta=round(total_fta / games, 1),
    )


def _to_pct(value) -> Optional[float]:
    """Convert a decimal value (0.234) to a percentage (23.4) for API response."""
    if value is None:
        return None
    return round(float(value) * 100, 1)


def _fetch_advanced_stats(player_id: int) -> Optional[AdvancedStatsData]:
    """Fetch the latest advanced stats for a player from the pipeline table.

    The NBA API stores percentage stats as decimals (e.g., USG_PCT=0.234).
    This function converts them to human-readable percentages (23.4) for the API response.
    """
    record = PlayerAdvancedStats.get_latest_for_player(player_id)
    if not record:
        return None

    return AdvancedStatsData(
        off_rating=float(record.off_rating) if record.off_rating is not None else None,
        def_rating=float(record.def_rating) if record.def_rating is not None else None,
        net_rating=float(record.net_rating) if record.net_rating is not None else None,
        usg_pct=_to_pct(record.usg_pct),
        ast_pct=_to_pct(record.ast_pct),
        ast_to_tov=float(record.ast_to_tov) if record.ast_to_tov is not None else None,
        reb_pct=_to_pct(record.reb_pct),
        oreb_pct=_to_pct(record.oreb_pct),
        dreb_pct=_to_pct(record.dreb_pct),
        tov_pct=_to_pct(record.tov_pct),
        pace=float(record.pace) if record.pace is not None else None,
        pie=_to_pct(record.pie),
        plus_minus=float(record.plus_minus) if record.plus_minus is not None else None,
    )


class PlayerService:

    @staticmethod
    async def get_player_stats(
        espn_id: Optional[int] = None,
        player_id: Optional[int] = None,
        name: Optional[str] = None,
        team: Optional[str] = None,
        window: str = "season",
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

            # Step 3: Apply window to compute averages
            window_size = _parse_window_size(window)
            if window_size is not None:
                windowed_logs = game_logs_list[-window_size:]
            else:
                windowed_logs = game_logs_list

            window_games = len(windowed_logs)
            avg_stats = _compute_avg_stats(windowed_logs)

            # Step 4: Fetch advanced stats from pipeline
            advanced_stats = _fetch_advanced_stats(player.id)

            # Step 5: Build full game logs (always return all for charts/tables)
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

            resolved_player_id = player.id

            player_stats = PlayerStats(
                id=resolved_player_id,
                name=player_name,
                team=player_team,
                games_played=games_played,
                window=window,
                window_games=window_games,
                avg_stats=avg_stats,
                advanced_stats=advanced_stats,
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

    @staticmethod
    async def get_player_percentiles(player_id: int, min_games: int = 20) -> PlayerPercentilesResp:
        log = get_logger()
        try:
            # Verify player exists
            player = Player.get_or_none(Player.id == player_id)
            if not player:
                return PlayerPercentilesResp(
                    status=ApiStatus.ERROR,
                    message="Player not found",
                    data=None,
                )

            # Get per-player season averages for all qualifying players
            stat_fields = {
                "avg_fpts": fn.AVG(PlayerGameStats.fpts),
                "avg_points": fn.AVG(PlayerGameStats.pts),
                "avg_rebounds": fn.AVG(PlayerGameStats.reb),
                "avg_assists": fn.AVG(PlayerGameStats.ast),
                "avg_steals": fn.AVG(PlayerGameStats.stl),
                "avg_blocks": fn.AVG(PlayerGameStats.blk),
                "avg_turnovers": fn.AVG(PlayerGameStats.tov),
                "avg_minutes": fn.AVG(PlayerGameStats.min),
                "avg_fg_pct": Case(
                    None,
                    [(fn.SUM(PlayerGameStats.fga) > 0,
                      fn.SUM(PlayerGameStats.fgm) * 100.0 / fn.SUM(PlayerGameStats.fga))],
                    0.0,
                ),
                "avg_fg3_pct": Case(
                    None,
                    [(fn.SUM(PlayerGameStats.fg3a) > 0,
                      fn.SUM(PlayerGameStats.fg3m) * 100.0 / fn.SUM(PlayerGameStats.fg3a))],
                    0.0,
                ),
                "avg_ft_pct": Case(
                    None,
                    [(fn.SUM(PlayerGameStats.fta) > 0,
                      fn.SUM(PlayerGameStats.ftm) * 100.0 / fn.SUM(PlayerGameStats.fta))],
                    0.0,
                ),
            }

            query = (
                PlayerGameStats.select(
                    PlayerGameStats.player_id,
                    fn.COUNT(PlayerGameStats.id).alias("games"),
                    *[expr.alias(name) for name, expr in stat_fields.items()],
                )
                .group_by(PlayerGameStats.player_id)
                .having(fn.COUNT(PlayerGameStats.id) >= min_games)
            )

            # Build distributions: {stat_name: [values]}
            all_players = list(query.dicts())
            if not all_players:
                return PlayerPercentilesResp(
                    status=ApiStatus.ERROR,
                    message="No qualifying players found",
                    data=None,
                )

            # Find target player's row
            target_row = None
            for row in all_players:
                if row["player_id"] == player_id:
                    target_row = row
                    break

            if target_row is None:
                return PlayerPercentilesResp(
                    status=ApiStatus.ERROR,
                    message=f"Player does not meet minimum games threshold ({min_games})",
                    data=None,
                )

            # Compute percentile for each stat
            # Percentile = (number of players with lower average / total players) * 100
            total = len(all_players)
            invert_stats = {"avg_turnovers"}  # lower is better

            percentiles = {}
            for stat_name in stat_fields:
                target_val = float(target_row[stat_name])
                if stat_name in invert_stats:
                    # For turnovers, fewer is better, so count players with HIGHER avg
                    lower_count = sum(1 for r in all_players if float(r[stat_name]) > target_val)
                else:
                    lower_count = sum(1 for r in all_players if float(r[stat_name]) < target_val)
                percentiles[stat_name] = round((lower_count / total) * 100)

            return PlayerPercentilesResp(
                status=ApiStatus.SUCCESS,
                message="Percentiles calculated successfully",
                data=PercentileData(**percentiles),
            )

        except Exception as e:
            log.error("get_player_percentiles_error", error=str(e), player_id=player_id)
            return PlayerPercentilesResp(
                status=ApiStatus.ERROR,
                message="Internal server error",
                data=None,
            )
