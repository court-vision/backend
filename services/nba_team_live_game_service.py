"""
Service for NBA team live/upcoming game data.
"""

from datetime import date, datetime, timedelta

import pytz

from core.logging import get_logger
from db.models.nba.games import Game
from db.models.nba.teams import NBATeam
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.player_injuries import PlayerInjury
from schemas.common import ApiStatus
from schemas.teams import (
    NBATeamLiveGameResp,
    NBATeamLiveGameData,
    TopPerformer,
    InjuredPlayer,
)


def _get_nba_today() -> date:
    """Return today's NBA game date in ET (before 6am = yesterday)."""
    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)
    if now_et.hour < 6:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


_GAME_STATUS_MAP = {1: "scheduled", 2: "in_progress", 3: "final"}


def _live_top_performers(game_id: str, team_player_ids: set[int], limit: int = 5) -> list[TopPerformer]:
    """Fetch top performers from live stats for a specific game and team."""
    rows = list(
        LivePlayerStats.select(LivePlayerStats, Player)
        .join(Player)
        .where(
            (LivePlayerStats.game_id == game_id)
            & (LivePlayerStats.player_id.in_(team_player_ids))
        )
        .order_by(LivePlayerStats.pts.desc())
        .limit(limit)
    )
    return [
        TopPerformer(
            player_id=r.player_id,
            name=r.player.name,
            pts=r.pts,
            reb=r.reb,
            ast=r.ast,
            stl=r.stl,
            blk=r.blk,
            min=r.min,
            fgm=r.fgm,
            fga=r.fga,
            fg3m=r.fg3m,
        )
        for r in rows
    ]


def _get_team_player_ids(team_id: str) -> set[int]:
    """Get all current player IDs on a team from the latest season stats."""
    latest_date = (
        PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
        .order_by(PlayerSeasonStats.as_of_date.desc())
        .limit(1)
        .scalar()
    )
    if not latest_date:
        return set()
    return {
        s.player_id
        for s in PlayerSeasonStats.select(PlayerSeasonStats.player)
        .where(
            (PlayerSeasonStats.team == team_id)
            & (PlayerSeasonStats.as_of_date == latest_date)
        )
    }


def _get_injured_players(team_id: str, player_ids: set[int]) -> list[InjuredPlayer]:
    """Get injury report for players on a team."""
    injured = []
    for injury in PlayerInjury.get_injured_players():
        if injury.player_id in player_ids:
            try:
                player = Player.get_by_id(injury.player_id)
                injured.append(
                    InjuredPlayer(
                        player_id=injury.player_id,
                        name=player.name,
                        status=injury.status,
                        injury_type=injury.injury_type,
                        expected_return=injury.expected_return.isoformat() if injury.expected_return else None,
                    )
                )
            except Exception:
                pass
    return injured


class NBATeamLiveGameService:
    """Service for retrieving live, recent, or upcoming game data for an NBA team."""

    @staticmethod
    async def get_live_game(team_abbrev: str) -> NBATeamLiveGameResp:
        """
        Get the most relevant game for a team:
          1. Today's game (live or scheduled)
          2. Most recent final game
          3. Next upcoming game

        For in-progress games, live scores and top performers are fetched
        from the NBA API directly and from live_player_stats respectively.

        Args:
            team_abbrev: Team abbreviation (e.g., 'LAL')

        Returns:
            NBATeamLiveGameResp with game data
        """
        log = get_logger()
        team_id = team_abbrev.upper()

        try:
            team = NBATeam.get_or_none(NBATeam.id == team_id)
            if not team:
                return NBATeamLiveGameResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Team '{team_id}' not found",
                    data=None,
                )

            nba_today = _get_nba_today()
            team_player_ids = _get_team_player_ids(team_id)

            # --- Check for today's game ---
            today_games = Game.get_team_games(team_id=team_id, start_date=nba_today, end_date=nba_today)
            if today_games:
                g = today_games[0]
                is_home = g.home_team_id == team_id

                home_score = g.home_score
                away_score = g.away_score
                status = g.status
                period = None
                game_clock = None
                home_periods: list[int] = []
                away_periods: list[int] = []
                home_performers: list[TopPerformer] = []
                away_performers: list[TopPerformer] = []

                # Overlay live data for in-progress or recently started games
                if g.status in ("in_progress", "scheduled"):
                    try:
                        from pipelines.extractors.nba_api import NBAApiExtractor
                        extractor = NBAApiExtractor()
                        box = extractor.get_live_box_score(g.game_id)
                        if box:
                            game_data = box.get("game", {})
                            home_team_data = game_data.get("homeTeam", {})
                            away_team_data = game_data.get("awayTeam", {})

                            home_score = home_team_data.get("score", home_score)
                            away_score = away_team_data.get("score", away_score)
                            period = game_data.get("period", period)
                            game_clock = game_data.get("gameClock", game_clock)

                            status_int = game_data.get("gameStatus", 1)
                            status = _GAME_STATUS_MAP.get(status_int, status)

                            # Quarter-by-quarter scores from completed periods
                            home_periods = [
                                p.get("score", 0)
                                for p in home_team_data.get("periods", [])
                            ]
                            away_periods = [
                                p.get("score", 0)
                                for p in away_team_data.get("periods", [])
                            ]
                    except Exception as e:
                        log.warning("live_box_score_fetch_failed", error=str(e), game_id=g.game_id)

                # Top performers from live_player_stats
                if g.game_id and status in ("in_progress", "final"):
                    home_player_ids = _get_team_player_ids(g.home_team_id)
                    away_player_ids = _get_team_player_ids(g.away_team_id)
                    home_performers = _live_top_performers(g.game_id, home_player_ids)
                    away_performers = _live_top_performers(g.game_id, away_player_ids)

                # Injury report for scheduled games
                injured: list[InjuredPlayer] = []
                if status == "scheduled":
                    injured = _get_injured_players(team_id, team_player_ids)

                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS,
                    message=f"Today's game for {team.name}",
                    data=NBATeamLiveGameData(
                        game_id=g.game_id,
                        game_date=g.game_date.isoformat(),
                        home_team=g.home_team_id,
                        away_team=g.away_team_id,
                        home_score=home_score,
                        away_score=away_score,
                        status=status,
                        period=period,
                        game_clock=game_clock,
                        start_time_et=g.start_time_et.strftime("%H:%M") if g.start_time_et else None,
                        arena=g.arena,
                        home_periods=home_periods,
                        away_periods=away_periods,
                        home_top_performers=home_performers,
                        away_top_performers=away_performers,
                        injured_players=injured,
                        is_today=True,
                        is_upcoming=status == "scheduled",
                    ),
                )

            # --- No game today: find most recent final or next upcoming ---
            all_games = Game.get_team_games(team_id=team_id)

            # Most recent final
            past_games = [g for g in all_games if g.status == "final" and g.game_date < nba_today]
            if past_games:
                g = past_games[-1]
                home_performers = _live_top_performers(g.game_id, _get_team_player_ids(g.home_team_id))
                away_performers = _live_top_performers(g.game_id, _get_team_player_ids(g.away_team_id))
                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS,
                    message=f"Most recent game for {team.name}",
                    data=NBATeamLiveGameData(
                        game_id=g.game_id,
                        game_date=g.game_date.isoformat(),
                        home_team=g.home_team_id,
                        away_team=g.away_team_id,
                        home_score=g.home_score,
                        away_score=g.away_score,
                        status=g.status,
                        start_time_et=g.start_time_et.strftime("%H:%M") if g.start_time_et else None,
                        arena=g.arena,
                        home_top_performers=home_performers,
                        away_top_performers=away_performers,
                        is_today=False,
                        is_upcoming=False,
                    ),
                )

            # Next upcoming game
            upcoming = [g for g in all_games if g.status == "scheduled" and g.game_date > nba_today]
            if upcoming:
                g = upcoming[0]
                injured = _get_injured_players(team_id, team_player_ids)
                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS,
                    message=f"Next game for {team.name}",
                    data=NBATeamLiveGameData(
                        game_id=g.game_id,
                        game_date=g.game_date.isoformat(),
                        home_team=g.home_team_id,
                        away_team=g.away_team_id,
                        status=g.status,
                        start_time_et=g.start_time_et.strftime("%H:%M") if g.start_time_et else None,
                        arena=g.arena,
                        injured_players=injured,
                        is_today=False,
                        is_upcoming=True,
                    ),
                )

            return NBATeamLiveGameResp(
                status=ApiStatus.NOT_FOUND,
                message=f"No games found for {team.name}",
                data=None,
            )

        except Exception as e:
            log.error("nba_team_live_game_fetch_error", error=str(e), team=team_id)
            return NBATeamLiveGameResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch game data",
                data=None,
            )
