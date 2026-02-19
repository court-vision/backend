"""
Notification API Routes

Endpoints for managing notification preferences and checking lineup issues.
Uses Clerk authentication (same as teams, matchups, etc.).
"""

import json
from datetime import date

from fastapi import APIRouter, Depends, Query

from core.clerk_auth import get_current_user
from db.models.users import User
from db.models.teams import Team
from db.models.nba.games import Game
from db.models.notifications import NotificationPreference, NotificationLog
from pipelines.extractors import ESPNExtractor
from services.lineup_check_service import LineupCheckService
from services.user_sync_service import UserSyncService
from schemas.common import ApiStatus
from schemas.notifications import (
    NotificationPreferenceReq,
    NotificationPreferenceResp,
    NotificationPreferenceResponse,
    LineupIssueResp,
    LineupCheckResp,
    LineupCheckResponse,
)


router = APIRouter(prefix="/notifications", tags=["notifications"])


def _get_user_id(current_user: dict) -> int:
    """Helper to get local user_id from Clerk user info."""
    clerk_user_id = current_user.get("clerk_user_id")
    email = current_user.get("email")
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return user.user_id


@router.get("/preferences", response_model=NotificationPreferenceResponse)
async def get_preferences(current_user: dict = Depends(get_current_user)):
    """Get the current user's notification preferences (or defaults)."""
    user_id = _get_user_id(current_user)

    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    if prefs:
        data = NotificationPreferenceResp(
            lineup_alerts_enabled=prefs.lineup_alerts_enabled,
            alert_benched_starters=prefs.alert_benched_starters,
            alert_active_non_playing=prefs.alert_active_non_playing,
            alert_injured_active=prefs.alert_injured_active,
            alert_minutes_before=prefs.alert_minutes_before,
            email=prefs.email,
        )
    else:
        # Return defaults
        data = NotificationPreferenceResp()

    return NotificationPreferenceResponse(
        status=ApiStatus.SUCCESS,
        message="Notification preferences retrieved",
        data=data,
    )


@router.put("/preferences", response_model=NotificationPreferenceResponse)
async def update_preferences(
    req: NotificationPreferenceReq,
    current_user: dict = Depends(get_current_user),
):
    """Create or update notification preferences for the current user."""
    user_id = _get_user_id(current_user)

    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    if prefs:
        prefs.lineup_alerts_enabled = req.lineup_alerts_enabled
        prefs.alert_benched_starters = req.alert_benched_starters
        prefs.alert_active_non_playing = req.alert_active_non_playing
        prefs.alert_injured_active = req.alert_injured_active
        prefs.alert_minutes_before = req.alert_minutes_before
        prefs.email = req.email
        prefs.save()
    else:
        prefs = NotificationPreference.create(
            user=user_id,
            lineup_alerts_enabled=req.lineup_alerts_enabled,
            alert_benched_starters=req.alert_benched_starters,
            alert_active_non_playing=req.alert_active_non_playing,
            alert_injured_active=req.alert_injured_active,
            alert_minutes_before=req.alert_minutes_before,
            email=req.email,
        )

    data = NotificationPreferenceResp(
        lineup_alerts_enabled=prefs.lineup_alerts_enabled,
        alert_benched_starters=prefs.alert_benched_starters,
        alert_active_non_playing=prefs.alert_active_non_playing,
        alert_injured_active=prefs.alert_injured_active,
        alert_minutes_before=prefs.alert_minutes_before,
        email=prefs.email,
    )

    return NotificationPreferenceResponse(
        status=ApiStatus.SUCCESS,
        message="Notification preferences updated",
        data=data,
    )


@router.get("/check-lineup/{team_id}", response_model=LineupCheckResponse)
async def check_lineup(
    team_id: int,
    current_user: dict = Depends(get_current_user),
):
    """
    Manually check lineup issues for a specific team.

    Returns lineup issues without sending a notification.
    Useful for on-demand checking from the frontend.
    """
    user_id = _get_user_id(current_user)

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return LineupCheckResponse(
            status=ApiStatus.NOT_FOUND,
            message="Team not found",
            data=None,
        )

    league_info = json.loads(team.league_info)
    provider = league_info.get("provider", "espn")

    if provider != "espn":
        return LineupCheckResponse(
            status=ApiStatus.ERROR,
            message="Only ESPN teams are supported for lineup checks",
            data=None,
        )

    # Get teams playing today
    from core.settings import settings
    today = date.today()
    teams_playing = Game.get_teams_playing_on_date(today)
    earliest_game_time = Game.get_earliest_game_time_on_date(today)

    # Fetch roster
    espn_extractor = ESPNExtractor()
    roster = espn_extractor.get_roster_with_slots(
        league_id=league_info["league_id"],
        team_name=league_info.get("team_name", ""),
        espn_s2=league_info.get("espn_s2", ""),
        swid=league_info.get("swid", ""),
        year=league_info.get("year", settings.espn_year),
    )

    if not roster:
        return LineupCheckResponse(
            status=ApiStatus.ERROR,
            message="Failed to fetch roster from ESPN",
            data=None,
        )

    # Get user's prefs
    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    # Check lineup
    checker = LineupCheckService()
    issues = checker.check_lineup(
        roster=roster,
        teams_playing_today=teams_playing,
        prefs=prefs,
    )

    team_name = league_info.get("team_name", "Your Team")

    data = LineupCheckResp(
        team_name=team_name,
        issues=[
            LineupIssueResp(
                issue_type=issue.issue_type.value,
                player_name=issue.player_name,
                player_team=issue.player_team,
                current_slot=issue.current_slot,
                suggested_action=issue.suggested_action,
                injury_status=issue.injury_status,
            )
            for issue in issues
        ],
        first_game_time=str(earliest_game_time) if earliest_game_time else None,
        teams_playing_today=sorted(teams_playing),
    )

    return LineupCheckResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(issues)} lineup issue(s)" if issues else "No lineup issues found",
        data=data,
    )


@router.get("/history")
async def get_notification_history(
    current_user: dict = Depends(get_current_user),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Get recent notification history for the current user."""
    user_id = _get_user_id(current_user)

    logs = (
        NotificationLog.select()
        .where(NotificationLog.user == user_id)
        .order_by(NotificationLog.created_at.desc())
        .limit(limit)
    )

    return {
        "status": ApiStatus.SUCCESS.value,
        "message": f"Found {len(logs)} notifications",
        "data": [
            {
                "id": str(log.id),
                "team_id": log.team_id,
                "notification_type": log.notification_type,
                "notification_date": str(log.notification_date),
                "status": log.status,
                "alert_data": json.loads(log.alert_data) if log.alert_data else None,
                "created_at": str(log.created_at),
                "sent_at": str(log.sent_at) if log.sent_at else None,
            }
            for log in logs
        ],
    }
