from typing import Optional
from schemas.player import PlayerStatsResp, PlayerStats, AvgStats, GameLog
from schemas.common import ApiStatus
from db.models.stats.daily_player_stats import DailyPlayerStats
from peewee import fn


class PlayerService:

    @staticmethod
    async def get_player_stats(
        player_id: Optional[int] = None,
        name: Optional[str] = None,
        team: Optional[str] = None
    ) -> PlayerStatsResp:
        try:
            # Build query based on provided parameters
            query = DailyPlayerStats.select()
            
            if player_id is not None:
                # Lookup by player ID (used for standings)
                query = query.where(DailyPlayerStats.id == player_id)
            elif name is not None:
                # Lookup by name (and optionally team) - used for roster
                query = query.where(DailyPlayerStats.name == name)
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

            avg_stats = AvgStats(
                avg_fpts=round(total_fpts / games_played, 1),
                avg_points=round(total_pts / games_played, 1),
                avg_rebounds=round(total_reb / games_played, 1),
                avg_assists=round(total_ast / games_played, 1),
                avg_steals=round(total_stl / games_played, 1),
                avg_blocks=round(total_blk / games_played, 1),
                avg_turnovers=round(total_tov / games_played, 1),
                avg_minutes=round(total_min / games_played, 1),
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

