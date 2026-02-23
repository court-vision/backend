"""
Service for game-related operations.
"""

from datetime import date, datetime, timedelta

import pytz

from services.schedule_service import get_upcoming_games_on_date
from core.logging import get_logger
from db.models.nba.games import Game
from schemas.common import ApiStatus
from schemas.games import GamesOnDateResp, GamesOnDateData, GameInfo


def _get_nba_today() -> date:
    """Return today's NBA game date in ET (before 6am = yesterday)."""
    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)
    if now_et.hour < 6:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


_GAME_STATUS_MAP = {1: "scheduled", 2: "in_progress", 3: "final"}


class GamesService:
    """Service for retrieving game information."""

    @staticmethod
    async def get_games_on_date(game_date: date) -> GamesOnDateResp:
        """
        Get all games on a specific date.

        For today's NBA date, live scoreboard data (scores, status, period,
        game_clock) is overlaid on top of the DB records so callers always
        get the freshest available state without a separate endpoint.

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
                        period=None,
                        game_clock=None,
                    )
                    for g in games
                ]

                # For today, overlay live scores/status/period/clock from the
                # NBA live scoreboard. The DB record may be stale since games
                # are only written once (at scheduling time) or once complete.
                if game_date == _get_nba_today():
                    from pipelines.extractors.nba_api import NBAApiExtractor
                    try:
                        extractor = NBAApiExtractor()
                        live_games = extractor.get_scoreboard_games(game_date)
                        live_by_id = {g["game_id"]: g for g in live_games}

                        game_list = [
                            GameInfo(
                                game_id=g.game_id,
                                game_date=g.game_date.isoformat(),
                                home_team=g.home_team_id,
                                away_team=g.away_team_id,
                                home_score=live_by_id[g.game_id]["home_score"] if g.game_id in live_by_id else g.home_score,
                                away_score=live_by_id[g.game_id]["away_score"] if g.game_id in live_by_id else g.away_score,
                                status=_GAME_STATUS_MAP.get(live_by_id[g.game_id]["game_status"], g.status) if g.game_id in live_by_id else g.status,
                                arena=g.arena,
                                period=live_by_id[g.game_id]["period"] if g.game_id in live_by_id else None,
                                game_clock=live_by_id[g.game_id]["game_clock"] if g.game_id in live_by_id else None,
                            )
                            for g in games
                        ]
                    except Exception as e:
                        log.warning("live_scoreboard_overlay_failed", error=str(e), date=game_date.isoformat())
                        # Fall through with stale DB data rather than failing the request
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
                        period=None,
                        game_clock=None,
                    )
                    for game in games
                    if game.get("homeTeam") and game.get("awayTeam")
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
