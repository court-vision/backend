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
from db.models.notifications import NotificationPreference, NotificationLog, NotificationTeamPreference
from pipelines.extractors import ESPNExtractor
from services.lineup_check_service import LineupCheckService
from services.notification_service import NotificationService
from services.user_sync_service import UserSyncService
from schemas.common import ApiStatus
from schemas.notifications import (
    NotificationPreferenceReq,
    NotificationPreferenceResp,
    NotificationPreferenceResponse,
    NotificationTeamPreferenceReq,
    NotificationTeamPreferenceResp,
    NotificationTeamPreferenceListResponse,
    NotificationTeamPreferenceSingleResponse,
    LineupIssueResp,
    LineupCheckResp,
    LineupCheckResponse,
)


router = APIRouter(prefix="/notifications", tags=["notifications"])

espn_extractor = ESPNExtractor()
lineup_checker = LineupCheckService()


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


@router.get("/team-preferences", response_model=NotificationTeamPreferenceListResponse)
async def get_team_preferences(current_user: dict = Depends(get_current_user)):
    """List all team-level notification preference overrides for the current user."""
    user_id = _get_user_id(current_user)

    rows = list(
        NotificationTeamPreference.select()
        .where(NotificationTeamPreference.user == user_id)
    )

    data = [
        NotificationTeamPreferenceResp(
            team_id=row.team_id,
            has_override=True,
            lineup_alerts_enabled=row.lineup_alerts_enabled,
            alert_benched_starters=row.alert_benched_starters,
            alert_active_non_playing=row.alert_active_non_playing,
            alert_injured_active=row.alert_injured_active,
            alert_minutes_before=row.alert_minutes_before,
            email=row.email,
        )
        for row in rows
    ]

    return NotificationTeamPreferenceListResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(data)} team preference override(s)",
        data=data,
    )


@router.put("/team-preferences/{team_id}", response_model=NotificationTeamPreferenceSingleResponse)
async def upsert_team_preference(
    team_id: int,
    req: NotificationTeamPreferenceReq,
    current_user: dict = Depends(get_current_user),
):
    """Create or update a team-level notification preference override."""
    user_id = _get_user_id(current_user)

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return NotificationTeamPreferenceSingleResponse(
            status=ApiStatus.NOT_FOUND,
            message="Team not found",
            data=None,
        )

    # Upsert
    existing = (
        NotificationTeamPreference.select()
        .where(
            (NotificationTeamPreference.user == user_id)
            & (NotificationTeamPreference.team_id == team_id)
        )
        .first()
    )

    if existing:
        existing.lineup_alerts_enabled = req.lineup_alerts_enabled
        existing.alert_benched_starters = req.alert_benched_starters
        existing.alert_active_non_playing = req.alert_active_non_playing
        existing.alert_injured_active = req.alert_injured_active
        existing.alert_minutes_before = req.alert_minutes_before
        existing.email = req.email
        existing.save()
        row = existing
    else:
        row = NotificationTeamPreference.create(
            user=user_id,
            team_id=team_id,
            lineup_alerts_enabled=req.lineup_alerts_enabled,
            alert_benched_starters=req.alert_benched_starters,
            alert_active_non_playing=req.alert_active_non_playing,
            alert_injured_active=req.alert_injured_active,
            alert_minutes_before=req.alert_minutes_before,
            email=req.email,
        )

    data = NotificationTeamPreferenceResp(
        team_id=row.team_id,
        has_override=True,
        lineup_alerts_enabled=row.lineup_alerts_enabled,
        alert_benched_starters=row.alert_benched_starters,
        alert_active_non_playing=row.alert_active_non_playing,
        alert_injured_active=row.alert_injured_active,
        alert_minutes_before=row.alert_minutes_before,
        email=row.email,
    )

    return NotificationTeamPreferenceSingleResponse(
        status=ApiStatus.SUCCESS,
        message="Team preference override saved",
        data=data,
    )


@router.delete("/team-preferences/{team_id}")
async def delete_team_preference(
    team_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Delete a team-level override, reverting that team to global defaults."""
    user_id = _get_user_id(current_user)

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return {"status": ApiStatus.NOT_FOUND.value, "message": "Team not found"}

    deleted = (
        NotificationTeamPreference.delete()
        .where(
            (NotificationTeamPreference.user == user_id)
            & (NotificationTeamPreference.team_id == team_id)
        )
        .execute()
    )

    if deleted:
        return {"status": ApiStatus.SUCCESS.value, "message": "Team preference override deleted"}
    else:
        return {"status": ApiStatus.NOT_FOUND.value, "message": "No override found for this team"}


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
    issues = lineup_checker.check_lineup(
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


@router.post("/send-test/{team_id}")
async def send_test_alert(
    team_id: int,
    current_user: dict = Depends(get_current_user),
    email: str = Query(..., description="Email address to send the test alert to"),
):
    """
    Force-send a lineup alert for a team, bypassing the time window check.

    Useful for testing Resend integration and verifying lineup issue detection.
    The notification log dedup is also bypassed so you can re-send freely.
    """
    user_id = _get_user_id(current_user)

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return {"status": "not_found", "message": "Team not found"}

    league_info = json.loads(team.league_info)
    provider = league_info.get("provider", "espn")

    if provider != "espn":
        return {"status": "error", "message": "Only ESPN teams are supported"}

    # Get today's game context
    from core.settings import settings as app_settings
    today = date.today()
    teams_playing = Game.get_teams_playing_on_date(today)
    earliest_game_time = Game.get_earliest_game_time_on_date(today)

    # Fetch roster from ESPN
    roster = espn_extractor.get_roster_with_slots(
        league_id=league_info["league_id"],
        team_name=league_info.get("team_name", ""),
        espn_s2=league_info.get("espn_s2", ""),
        swid=league_info.get("swid", ""),
        year=league_info.get("year", app_settings.espn_year),
    )

    if not roster:
        return {"status": "error", "message": "Failed to fetch roster from ESPN"}

    # Get prefs (for issue type filtering)
    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    # Check lineup issues
    issues = lineup_checker.check_lineup(
        roster=roster,
        teams_playing_today=teams_playing,
        prefs=prefs,
    )

    team_name = league_info.get("team_name", "Your Team")

    if not issues:
        # Still send a "no issues" test email so we can verify delivery
        from dataclasses import dataclass
        # Build a dummy user-like object with the override email
        class _FakeUser:
            email = None

        fake_user = _FakeUser()
        fake_user.email = email

        notification_svc = NotificationService()
        result = notification_svc._send_email(
            to=email,
            subject=f"Court Vision Test: No lineup issues for {team_name}",
            body=f"Team: {team_name}\nFirst game today: {earliest_game_time or 'No games today'}\n\nNo lineup issues found — your roster looks good!\n\n-- Court Vision",
        )
        return {
            "status": "sent",
            "message": "No lineup issues found. Sent confirmation email.",
            "issues": [],
            "email_result": {"success": result.success, "message_id": result.message_id, "error": result.error},
            "teams_playing_today": sorted(teams_playing),
            "first_game_time": str(earliest_game_time) if earliest_game_time else None,
        }

    # Send with the override email
    class _UserWithEmail:
        def __init__(self, uid, mail):
            self.user_id = uid
            self.email = mail

    user_obj = _UserWithEmail(user_id, email)

    # Override prefs email for this test
    class _PrefsWithEmail:
        def __init__(self, base_prefs):
            self.email = email
            self.alert_benched_starters = getattr(base_prefs, "alert_benched_starters", True)
            self.alert_active_non_playing = getattr(base_prefs, "alert_active_non_playing", True)
            self.alert_injured_active = getattr(base_prefs, "alert_injured_active", True)

    test_prefs = _PrefsWithEmail(prefs)

    notification_svc = NotificationService()
    result = notification_svc.send_lineup_alert(
        user=user_obj,
        team=team,
        issues=issues,
        first_game_time=earliest_game_time,
        prefs=test_prefs,
    )

    return {
        "status": "sent" if result.success else "failed",
        "message": f"Alert sent with {len(issues)} issue(s)" if result.success else f"Send failed: {result.error}",
        "issues": [
            {
                "type": issue.issue_type.value,
                "player": issue.player_name,
                "team": issue.player_team,
                "slot": issue.current_slot,
                "action": issue.suggested_action,
            }
            for issue in issues
        ],
        "email_result": {"success": result.success, "message_id": result.message_id, "error": result.error},
        "teams_playing_today": sorted(teams_playing),
        "first_game_time": str(earliest_game_time) if earliest_game_time else None,
    }


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
