from services.espn_service import EspnService
from services.team_service import TeamService
from schemas.matchup import MatchupResp
from schemas.common import ApiStatus, LeagueInfo


class MatchupService:
    """Service for handling matchup-related operations."""

    @staticmethod
    async def get_current_matchup(
        league_info: LeagueInfo,
        avg_window: str = "season"
    ) -> MatchupResp:
        """
        Get current matchup data for a team using league credentials.

        Args:
            league_info: League credentials and team information
            avg_window: Averaging window for projections (season, last_7, last_14, last_30)

        Returns:
            MatchupResp with matchup data or error
        """
        return await EspnService.get_matchup_data(league_info, avg_window)

    @staticmethod
    async def get_matchup_by_team_id(
        user_id: int,
        team_id: int,
        avg_window: str = "season"
    ) -> MatchupResp:
        """
        Get current matchup data for a saved team.

        Args:
            user_id: The user's ID (for authorization)
            team_id: The saved team's ID
            avg_window: Averaging window for projections

        Returns:
            MatchupResp with matchup data or error
        """
        # Get the team's league info
        team_resp = await TeamService.view_team(team_id)

        if team_resp.status != ApiStatus.SUCCESS or not team_resp.data:
            return MatchupResp(
                status=ApiStatus.NOT_FOUND,
                message=f"Team with ID {team_id} not found",
                data=None
            )

        league_info = team_resp.data.league_info

        # Fetch matchup data using the league info
        return await EspnService.get_matchup_data(league_info, avg_window)
