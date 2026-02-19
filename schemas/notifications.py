"""
Notification Schemas

Pydantic models for notification preferences and lineup check responses.
"""

from typing import Optional

from pydantic import BaseModel, Field

from schemas.common import BaseResponse


# ------------------------------- Request Models ------------------------------- #


class NotificationPreferenceReq(BaseModel):
    """Request model for creating/updating notification preferences."""
    lineup_alerts_enabled: bool = True
    alert_benched_starters: bool = True
    alert_active_non_playing: bool = True
    alert_injured_active: bool = True
    alert_minutes_before: int = Field(default=90, ge=15, le=180)
    email: Optional[str] = None


# ------------------------------- Response Data Models ------------------------------- #


class NotificationPreferenceResp(BaseModel):
    """Notification preference data."""
    lineup_alerts_enabled: bool = True
    alert_benched_starters: bool = True
    alert_active_non_playing: bool = True
    alert_injured_active: bool = True
    alert_minutes_before: int = Field(default=90, ge=15, le=180)
    email: Optional[str] = None


class LineupIssueResp(BaseModel):
    """Single lineup issue."""
    issue_type: str
    player_name: str
    player_team: str
    current_slot: str
    suggested_action: str
    injury_status: Optional[str] = None


class LineupCheckResp(BaseModel):
    """Lineup check result for a team."""
    team_name: str
    issues: list[LineupIssueResp]
    first_game_time: Optional[str] = None
    teams_playing_today: list[str]


# ------------------------------- Response Wrappers ------------------------------- #


class NotificationPreferenceResponse(BaseResponse):
    """API response wrapper for notification preferences."""
    data: Optional[NotificationPreferenceResp] = None


class LineupCheckResponse(BaseResponse):
    """API response wrapper for lineup check results."""
    data: Optional[LineupCheckResp] = None
