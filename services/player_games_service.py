"""
Service for player game log operations.
"""

from core.logging import get_logger
from db.models.nba.players import Player
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.games import Game
from db.base import DB_RUNTIME_ERRORS, db_operation
from schemas.common import ApiStatus
from schemas.player_games import PlayerGamesResp, PlayerGamesData, GameLog


class PlayerGamesService:
    """Service for retrieving player game logs."""

    @staticmethod
    @db_operation("players.games")
    def get_player_games(
        player_id: int,
        limit: int = 10,
    ) -> PlayerGamesResp:
        """
        Get game log for a player.

        Args:
            player_id: NBA player ID
            limit: Maximum number of games to return

        Returns:
            PlayerGamesResp with game log
        """
        log = get_logger()

        try:
            # Get player info
            player = Player.get_or_none(Player.id == player_id)
            if not player:
                return PlayerGamesResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Player with ID {player_id} not found",
                    data=None,
                )

            # Get game stats
            games = PlayerGameStats.get_player_games(player_id=player_id, limit=limit)

            # Game stats currently have no game_id FK. Resolve in one batch by
            # the team the player represented that day, including traded players.
            matchups = {}
            if games:
                teams = {g.team_id for g in games if g.team_id}
                for matchup in Game.select().where(
                    (Game.game_date.in_([g.game_date for g in games]))
                    & ((Game.home_team.in_(teams)) | (Game.away_team.in_(teams)))
                ):
                    for team_id in (matchup.home_team_id, matchup.away_team_id):
                        matchups.setdefault((matchup.game_date, team_id), []).append(matchup)

            def context(stats):
                candidates = matchups.get((stats.game_date, stats.team_id), [])
                if len(candidates) != 1:
                    return {}
                matchup = candidates[0]
                home = matchup.home_team_id == stats.team_id
                return {
                    "game_id": matchup.game_id,
                    "home": home,
                    "opponent": matchup.away_team_id if home else matchup.home_team_id,
                }

            game_logs = [
                GameLog(
                    date=g.game_date.isoformat(),
                    **context(g),
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
                for g in games
            ]

            # Get current team from most recent game
            current_team = games[0].team_id if games else None

            return PlayerGamesResp(
                status=ApiStatus.SUCCESS,
                message=f"Game log for {player.name}",
                data=PlayerGamesData(
                    player_id=player_id,
                    player_name=player.name,
                    team=current_team,
                    games=game_logs,
                    total_games=len(game_logs),
                ),
            )

        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            log.error("player_games_error", error=str(e), player_id=player_id)
            return PlayerGamesResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch player games",
                data=None,
            )
