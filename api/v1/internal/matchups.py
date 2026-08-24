from fastapi import APIRouter, Depends, Query
from services.matchup_service import MatchupService
from schemas.matchup import MatchupReq, MatchupResp, MatchupScoreHistoryResp, LiveMatchupResp, DailyMatchupResp, WeeklyMatchupResp, SeasonSummaryResp
from core.clerk_auth import get_current_user
from api.deps import get_owned_team
from db.models.teams import Team


router = APIRouter(prefix="/matchups", tags=["matchups"])


@router.post('/current', response_model=MatchupResp)
async def get_current_matchup(
    matchup_req: MatchupReq,
    _: dict = Depends(get_current_user)
) -> MatchupResp:
    """
    Get current week's matchup with projections.

    Requires league_info with credentials and team name.
    Returns both teams' rosters, current scores, and projected final scores.
    """
    return await MatchupService.get_current_matchup(
        matchup_req.league_info,
        matchup_req.avg_window
    )


@router.get('/current/{team_id}', response_model=MatchupResp)
async def get_matchup_by_team(
    avg_window: str = Query(
        default="season",
        pattern="^(season|last_7|last_14|last_30)$",
        description="Averaging window: season, last_7, last_14, or last_30"
    ),
    team: Team = Depends(get_owned_team)
) -> MatchupResp:
    """
    Get matchup for a saved team using the team's stored league info.

    This endpoint is convenient when you have a saved team and don't want
    to pass all the league credentials again.
    """
    return await MatchupService.get_matchup_by_team_id(
        team.user_id_id,
        team.team_id,
        avg_window
    )


@router.get('/live/{team_id}', response_model=LiveMatchupResp)
async def get_live_matchup(
    team: Team = Depends(get_owned_team)
) -> LiveMatchupResp:
    """
    Get the current matchup with live in-game stats per player.

    Combines ESPN/Yahoo live scores (correct for custom league scoring) with
    per-player box score stats from the live polling pipeline (~60s cadence).
    Players with no game today have live=null. Includes all roster slots
    (active and bench) so the frontend can render the full matchup layout.
    """
    return await MatchupService.get_live_matchup_by_team_id(team.user_id_id, team.team_id)


@router.get('/history/{team_id}', response_model=MatchupScoreHistoryResp)
async def get_matchup_score_history(
    matchup_period: int | None = Query(
        default=None,
        description="Specific matchup period (week number). If omitted, returns the latest."
    ),
    team: Team = Depends(get_owned_team)
) -> MatchupScoreHistoryResp:
    """
    Get daily score history for a team's matchup period.

    Returns historical daily snapshots of both teams' scores for charting
    the score progression over time.
    """
    return await MatchupService.get_score_history(team.team_id, matchup_period)


@router.get('/week/{team_id}', response_model=WeeklyMatchupResp)
async def get_weekly_matchup(
    team: Team = Depends(get_owned_team)
) -> WeeklyMatchupResp:
    """
    Get all days in the current matchup period in a single request.

    Makes one ESPN/Yahoo API call and returns per-day player data for every
    day in the matchup period. Use this instead of N parallel getDailyMatchup
    calls when rendering the matchup bar chart.
    """
    return await MatchupService.get_weekly_matchup(team.user_id_id, team.team_id)


@router.get('/season-summary/{team_id}', response_model=SeasonSummaryResp)
async def get_season_summary(
    team: Team = Depends(get_owned_team)
) -> SeasonSummaryResp:
    """
    Get the full-season W/L record, total points, and per-week results for a team.

    Aggregates DailyMatchupScore snapshots across all matchup periods.
    The last snapshot per period is used as the final score for that week.
    """
    return await MatchupService.get_season_summary(team.team_id)


@router.get('/daily/{team_id}', response_model=DailyMatchupResp)
async def get_daily_matchup(
    date: str = Query(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Target date in YYYY-MM-DD format"
    ),
    team: Team = Depends(get_owned_team)
) -> DailyMatchupResp:
    """
    Get daily drill-down for a matchup day.

    For past dates: returns player box score stats from player_game_stats.
    For future dates: returns which players have games scheduled.
    For today: returns stats so far (frontend should prefer live endpoint).
    """
    from datetime import date as date_type
    target_date = date_type.fromisoformat(date)
    return await MatchupService.get_daily_matchup(team.user_id_id, team.team_id, target_date)
