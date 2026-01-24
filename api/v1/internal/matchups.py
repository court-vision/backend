from fastapi import APIRouter, Depends, Query
from services.matchup_service import MatchupService
from services.user_sync_service import UserSyncService
from schemas.matchup import MatchupReq, MatchupResp, MatchupScoreHistoryResp
from core.clerk_auth import get_current_user


router = APIRouter(prefix="/matchups", tags=["matchups"])


def _get_user_id(current_user: dict) -> int:
    """Helper to get local user_id from Clerk user info."""
    clerk_user_id = current_user.get("clerk_user_id")
    email = current_user.get("email")
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return user.user_id


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
    team_id: int,
    avg_window: str = Query(
        default="season",
        pattern="^(season|last_7|last_14|last_30)$",
        description="Averaging window: season, last_7, last_14, or last_30"
    ),
    current_user: dict = Depends(get_current_user)
) -> MatchupResp:
    """
    Get matchup for a saved team using the team's stored league info.

    This endpoint is convenient when you have a saved team and don't want
    to pass all the league credentials again.
    """
    user_id = _get_user_id(current_user)
    return await MatchupService.get_matchup_by_team_id(
        user_id,
        team_id,
        avg_window
    )


@router.get('/history/{team_id}', response_model=MatchupScoreHistoryResp)
async def get_matchup_score_history(
    team_id: int,
    matchup_period: int | None = Query(
        default=None,
        description="Specific matchup period (week number). If omitted, returns the latest."
    ),
    _: dict = Depends(get_current_user)
) -> MatchupScoreHistoryResp:
    """
    Get daily score history for a team's matchup period.

    Returns historical daily snapshots of both teams' scores for charting
    the score progression over time.
    """
    return await MatchupService.get_score_history(team_id, matchup_period)
