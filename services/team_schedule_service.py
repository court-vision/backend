"""
Service for NBA team schedule operations.
"""

from datetime import date, timedelta

from core.logging import get_logger
from core.nba_calendar import nba_date_et
from core.settings import get_settings
from db.models.nba.games import FANTASY_SEASON_TYPES, Game
from db.models.nba.teams import NBATeam
from db.models.nba.team_stats import TeamStats
from db.base import DB_RUNTIME_ERRORS, db_operation
from schemas.common import ApiStatus
from schemas.teams import TeamScheduleResp, TeamScheduleData, ScheduleGame


class TeamScheduleService:
    """Service for retrieving NBA team schedules."""

    @staticmethod
    @db_operation("nba_team.schedule")
    def get_team_schedule(
        team_abbrev: str,
        upcoming: bool = False,
        limit: int = 20,
    ) -> TeamScheduleResp:
        """
        Get schedule for a team.

        Args:
            team_abbrev: Team abbreviation (e.g., 'LAL')
            upcoming: If True, only return future games
            limit: Maximum number of games to return

        Returns:
            TeamScheduleResp with schedule data
        """
        log = get_logger()
        team_id = team_abbrev.upper()

        try:
            # Verify team exists
            team = NBATeam.get_or_none(NBATeam.id == team_id)
            if not team:
                return TeamScheduleResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Team '{team_id}' not found",
                    data=None,
                )

            # Get games filtered to current season.
            # Use regular season start date to exclude preseason games
            # (which share the same season tag but consume limit slots).
            from services.schedule_service import get_matchup_by_number
            settings = get_settings()
            if upcoming:
                start_date = nba_date_et()
            else:
                matchup_1 = get_matchup_by_number(1)
                start_date = matchup_1["start_date"] if matchup_1 else None
            games = Game.get_team_games(
                team_id=team_id,
                start_date=start_date,
                season=settings.nba_season,
                # The fantasy schedule: no play-in or playoff games (migration 0031).
                season_types=FANTASY_SEASON_TYPES,
            )

            # Limit results
            games = games[:limit]

            # Back-to-backs from one query instead of an EXISTS per game (82 of
            # them for a full season, 90-110 ms of a team sheet in production):
            # every date the team played from the day before its first listed
            # game, in any season or season type, as Game.is_back_to_back asked
            # it. A preseason game the night before opening night still counts.
            one_day = timedelta(days=1)
            played_on = (
                Game.team_game_dates(team_id, games[0].game_date - one_day, games[-1].game_date - one_day)
                if games else set()
            )

            # Build def_rating lookup: {team_abbrev: def_rating}
            def_rating_map: dict[str, float] = {}
            try:
                def_rating_map = TeamStats.latest_def_ratings()
            except DB_RUNTIME_ERRORS:
                # Not swallowed: the route caches a success for five minutes,
                # so a dropped connection here would blank every opponent's
                # rating that long, on this replica and in every browser that
                # kept the copy. Failing the request (a 503) caches nothing.
                raise
            except Exception:
                pass  # Non-fatal; games still render without def_rating

            schedule = []
            for g in games:
                is_home = g.home_team_id == team_id
                opponent = g.away_team_id if is_home else g.home_team_id
                team_score = g.home_score if is_home else g.away_score
                opponent_score = g.away_score if is_home else g.home_score

                schedule.append(
                    ScheduleGame(
                        date=g.game_date.isoformat(),
                        opponent=opponent,
                        home=is_home,
                        back_to_back=(g.game_date - one_day) in played_on,
                        status=g.status,
                        team_score=team_score,
                        opponent_score=opponent_score,
                        opponent_def_rating=def_rating_map.get(opponent),
                    )
                )

            # Count remaining games
            remaining = sum(1 for g in schedule if g.status == "scheduled")

            return TeamScheduleResp(
                status=ApiStatus.SUCCESS,
                message=f"Schedule for {team.name}",
                data=TeamScheduleData(
                    team=team_id,
                    team_name=team.name,
                    schedule=schedule,
                    remaining_games=remaining,
                    total_games=len(schedule),
                ),
            )

        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            log.error("team_schedule_fetch_error", error=str(e), team=team_id)
            return TeamScheduleResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch team schedule",
                data=None,
            )
