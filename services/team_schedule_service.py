"""
Service for NBA team schedule operations.
"""

from datetime import date

from core.logging import get_logger
from core.settings import get_settings
from db.models.nba.games import Game
from db.models.nba.teams import NBATeam
from db.models.nba.team_stats import TeamStats
from schemas.common import ApiStatus
from schemas.teams import TeamScheduleResp, TeamScheduleData, ScheduleGame


class TeamScheduleService:
    """Service for retrieving NBA team schedules."""

    @staticmethod
    async def get_team_schedule(
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
                start_date = date.today()
            else:
                matchup_1 = get_matchup_by_number(1)
                start_date = matchup_1["start_date"] if matchup_1 else None
            games = Game.get_team_games(
                team_id=team_id,
                start_date=start_date,
                season=settings.nba_season,
            )

            # Limit results
            games = games[:limit]

            # Build def_rating lookup: {team_abbrev: def_rating}
            def_rating_map: dict[str, float] = {}
            try:
                for ts in TeamStats.get_all_latest():
                    if ts.def_rating is not None:
                        def_rating_map[ts.team_id] = float(ts.def_rating)
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
                        back_to_back=Game.is_back_to_back(team_id, g.game_date),
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

        except Exception as e:
            log.error("team_schedule_fetch_error", error=str(e), team=team_id)
            return TeamScheduleResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch team schedule",
                data=None,
            )
