"""
Service for NBA team schedule operations.
"""

from datetime import date

from core.logging import get_logger
from db.models.nba.games import Game
from db.models.nba.teams import NBATeam
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

            # Get games
            start_date = date.today() if upcoming else None
            games = Game.get_team_games(team_id=team_id, start_date=start_date)

            # Limit results
            games = games[:limit]

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
