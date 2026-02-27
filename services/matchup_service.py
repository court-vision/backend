from datetime import datetime, timedelta

from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.team_service import TeamService
from services.schedule_service import get_current_matchup as _get_schedule_matchup
from schemas.matchup import (
    MatchupResp,
    MatchupScoreHistoryResp,
    MatchupScoreHistory,
    DailyScorePoint,
    LiveMatchupResp,
    LiveMatchupData,
    LiveMatchupTeam,
    LiveMatchupPlayer,
    PlayerLiveStats,
)
from schemas.common import ApiStatus, LeagueInfo, FantasyProvider
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
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window)
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

        # Fetch matchup data using the league info - route by provider
        # Pass team_id for Yahoo so tokens can be refreshed and persisted
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window, team_id)
        return await EspnService.get_matchup_data(league_info, avg_window)

    @staticmethod
    async def get_live_matchup_by_team_id(
        user_id: int,
        team_id: int,
    ) -> LiveMatchupResp:
        """
        Get the current matchup augmented with live in-game stats per player.

        Fetches the current matchup (ESPN/Yahoo scores + roster) then overlays
        live stats from live_player_stats for each roster player matched by name.
        """
        import pytz
        from db.models.nba.live_player_stats import LivePlayerStats as LiveStatsModel

        # Reuse existing matchup fetch (handles ESPN and Yahoo routing)
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return LiveMatchupResp(status=matchup.status, message=matchup.message, data=None)

        # Today's NBA game date (before 6am ET = yesterday)
        eastern = pytz.timezone("US/Eastern")
        now_et = datetime.now(eastern)
        game_date = (now_et - timedelta(days=1)).date() if now_et.hour < 6 else now_et.date()

        espn_matchup_period = matchup.data.matchup_period

        # Use daily_matchup_scores as the canonical baseline (settled, authoritative).
        # Fall back to ESPN's totalPoints if the pipeline hasn't run yet for this period.
        baseline = (
            DailyMatchupScore
            .select()
            .where(
                (DailyMatchupScore.team_id == team_id) &
                (DailyMatchupScore.matchup_period == espn_matchup_period)
            )
            .order_by(DailyMatchupScore.date.desc())
            .first()
        )

        your_base = float(baseline.current_score) if baseline else matchup.data.your_team.current_score
        opponent_base = float(baseline.opponent_current_score) if baseline else matchup.data.opponent_team.current_score

        # Guard: only overlay live stats when game_date falls within the current ESPN matchup week.
        # This prevents old-week bleed at the week boundary.
        #
        # NOTE: We do NOT gate on whether a today-dated pipeline snapshot exists. The
        # DailyMatchupScoresPipeline runs at 10am ET (before any games start), so a
        # baseline.date == today simply means "the morning snapshot ran" — it does NOT
        # mean today's games are already counted in ESPN's totalPoints. Blocking on that
        # check causes live stats to be suppressed all evening.
        # Double-counting prevention: compute_live_score only sums game_status == 2
        # (in-progress). Final games (status 3) are already settled into the baseline
        # via daily_matchup_scores and are excluded. The live pipeline also cleans up
        # stale records from previous game days on each run.
        schedule_matchup = _get_schedule_matchup(game_date)
        week_matches = (
            schedule_matchup is not None
            and schedule_matchup["matchup_number"] == espn_matchup_period
        )
        include_live = week_matches

        if include_live:
            all_names = [
                p.name for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster
            ]
            live_stats_list = LiveStatsModel.get_live_stats_by_names(all_names, game_date)
            name_to_live = {stat.player.name_normalized: stat for stat in live_stats_list}
        else:
            name_to_live = {}

        def build_live_roster(roster) -> list[LiveMatchupPlayer]:
            result = []
            for p in roster:
                stat = name_to_live.get(p.name.lower().strip())
                live_overlay = None
                if stat:
                    live_overlay = PlayerLiveStats(
                        nba_player_id=stat.player_id,
                        live_fpts=stat.fpts,
                        live_pts=stat.pts,
                        live_reb=stat.reb,
                        live_ast=stat.ast,
                        live_stl=stat.stl,
                        live_blk=stat.blk,
                        live_tov=stat.tov,
                        live_min=stat.min,
                        game_status=stat.game_status,
                        period=stat.period,
                        game_clock=stat.game_clock,
                        last_updated=stat.last_updated.isoformat() if stat.last_updated else None,
                    )
                result.append(LiveMatchupPlayer(**p.model_dump(), live=live_overlay))
            return result

        def compute_live_score(base: float, live_roster: list[LiveMatchupPlayer]) -> float:
            """
            Add today's live fpts for in-progress roster players (not BE/IR) on
            top of the pipeline baseline score. Only game_status == 2 (in-progress)
            is counted — final games (status 3) are already settled into the
            daily_matchup_scores baseline, so including them would double-count.
            With name_to_live={}, today_fpts=0 and base is returned unchanged.
            """
            today_fpts = sum(
                p.live.live_fpts
                for p in live_roster
                if p.lineup_slot not in ("BE", "IR")
                and p.live is not None
                and p.live.game_status == 2
            )
            return round(base + today_fpts, 2)

        your_live_roster = build_live_roster(matchup.data.your_team.roster)
        opponent_live_roster = build_live_roster(matchup.data.opponent_team.roster)

        your_team = LiveMatchupTeam(
            team_name=matchup.data.your_team.team_name,
            team_id=matchup.data.your_team.team_id,
            current_score=compute_live_score(your_base, your_live_roster),
            projected_score=matchup.data.your_team.projected_score,
            roster=your_live_roster,
        )
        opponent_team = LiveMatchupTeam(
            team_name=matchup.data.opponent_team.team_name,
            team_id=matchup.data.opponent_team.team_id,
            current_score=compute_live_score(opponent_base, opponent_live_roster),
            projected_score=matchup.data.opponent_team.projected_score,
            roster=opponent_live_roster,
        )

        return LiveMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Live matchup data fetched successfully",
            data=LiveMatchupData(
                matchup_period=matchup.data.matchup_period,
                matchup_period_start=matchup.data.matchup_period_start,
                matchup_period_end=matchup.data.matchup_period_end,
                your_team=your_team,
                opponent_team=opponent_team,
                projected_winner=matchup.data.projected_winner,
                projected_margin=matchup.data.projected_margin,
                game_date=str(game_date),
            ),
        )

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
