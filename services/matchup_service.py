from services.espn_service import EspnService
from services.team_service import TeamService
from schemas.matchup import (
    MatchupResp,
    MatchupScoreHistoryResp,
    MatchupScoreHistory,
    DailyScorePoint
)
from schemas.common import ApiStatus, LeagueInfo
from db.models.stats.daily_matchup_score import DailyMatchupScore


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

    @staticmethod
    async def get_score_history(
        team_id: int,
        matchup_period: int | None = None
    ) -> MatchupScoreHistoryResp:
        """
        Get daily score history for a team's matchup period.

        Args:
            team_id: The team's ID
            matchup_period: Specific matchup period (week). If None, returns current/latest.

        Returns:
            MatchupScoreHistoryResp with daily score snapshots for charting
        """
        try:
            query = (
                DailyMatchupScore
                .select()
                .where(DailyMatchupScore.team_id == team_id)
            )

            if matchup_period is not None:
                query = query.where(DailyMatchupScore.matchup_period == matchup_period)
            else:
                # Get the latest matchup period for this team
                latest = (
                    DailyMatchupScore
                    .select(DailyMatchupScore.matchup_period)
                    .where(DailyMatchupScore.team_id == team_id)
                    .order_by(DailyMatchupScore.matchup_period.desc())
                    .limit(1)
                    .first()
                )
                if not latest:
                    return MatchupScoreHistoryResp(
                        status=ApiStatus.NOT_FOUND,
                        message="No score history found for this team",
                        data=None
                    )
                query = query.where(DailyMatchupScore.matchup_period == latest.matchup_period)

            records = list(query.order_by(DailyMatchupScore.day_of_matchup.asc()))

            if not records:
                return MatchupScoreHistoryResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No score history found for this matchup period",
                    data=None
                )

            first_record = records[0]
            history = [
                DailyScorePoint(
                    date=record.date.isoformat(),
                    day_of_matchup=record.day_of_matchup,
                    your_score=float(record.current_score),
                    opponent_score=float(record.opponent_current_score)
                )
                for record in records
            ]

            return MatchupScoreHistoryResp(
                status=ApiStatus.SUCCESS,
                message="Score history retrieved successfully",
                data=MatchupScoreHistory(
                    team_id=team_id,
                    team_name=first_record.team_name,
                    opponent_team_name=first_record.opponent_team_name,
                    matchup_period=first_record.matchup_period,
                    history=history
                )
            )

        except Exception as e:
            return MatchupScoreHistoryResp(
                status=ApiStatus.ERROR,
                message=f"Failed to fetch score history: {str(e)}",
                data=None
            )
