"""
Service for game-related operations.
"""

from datetime import date, datetime

from services.schedule_service import get_upcoming_games_on_date
from core.logging import get_logger
from db.models.nba.games import Game
from schemas.common import ApiStatus
from schemas.games import GamesOnDateResp, GamesOnDateData, GameInfo


class GamesService:
    """Service for retrieving game information."""

    @staticmethod
    async def get_games_on_date(game_date: date) -> GamesOnDateResp:
        """
        Get all games on a specific date.

        Args:
            game_date: Date to query

        Returns:
            GamesOnDateResp with list of games
        """
        log = get_logger()

        try:
            games = Game.get_games_on_date(game_date)

            if games:
                game_list = [
                    GameInfo(
                        game_id=g.game_id,
                        game_date=g.game_date.isoformat(),
                        home_team=g.home_team_id,
                        away_team=g.away_team_id,
                        home_score=g.home_score,
                        away_score=g.away_score,
                        status=g.status,
                        arena=g.arena,
                    )
                    for g in games
                ]
            else:
                games = get_upcoming_games_on_date(game_date)
                game_list = [
                    GameInfo(
                        game_id=None,
                        game_date=game_date.isoformat(),
                        home_team=game["homeTeam"],
                        away_team=game["awayTeam"],
                        home_score=None,
                        away_score=None,
                        status="scheduled",
                        arena=None,
                    )
                    for game in games
                ]

            return GamesOnDateResp(
                status=ApiStatus.SUCCESS,
                message=f"Found {len(game_list)} games on {game_date.isoformat()}",
                data=GamesOnDateData(
                    date=game_date.isoformat(),
                    games=game_list,
                    count=len(game_list),
                ),
            )

        except Exception as e:
            log.error("games_fetch_error", error=str(e), date=game_date.isoformat())
            return GamesOnDateResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch games",
                data=None,
            )
