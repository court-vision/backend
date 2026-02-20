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
    lineup_alerts_enabled: bool = False
    alert_benched_starters: bool = True
    alert_active_non_playing: bool = True
    alert_injured_active: bool = True
    alert_minutes_before: int = Field(default=90, ge=15, le=180)
    email: Optional[str] = None


class NotificationTeamPreferenceReq(BaseModel):
    """Request model — all fields optional; only supplied fields are overridden."""
    lineup_alerts_enabled: Optional[bool] = None
    alert_benched_starters: Optional[bool] = None
    alert_active_non_playing: Optional[bool] = None
    alert_injured_active: Optional[bool] = None
    alert_minutes_before: Optional[int] = Field(default=None, ge=15, le=180)
    email: Optional[str] = None


class NotificationTeamPreferenceResp(BaseModel):
    """Team-level notification preference override data."""
    team_id: int
    has_override: bool
    lineup_alerts_enabled: Optional[bool] = None
    alert_benched_starters: Optional[bool] = None
    alert_active_non_playing: Optional[bool] = None
    alert_injured_active: Optional[bool] = None
    alert_minutes_before: Optional[int] = None
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


class NotificationTeamPreferenceListResponse(BaseResponse):
    """API response wrapper for listing team preference overrides."""
    data: Optional[list[NotificationTeamPreferenceResp]] = None


class NotificationTeamPreferenceSingleResponse(BaseResponse):
    """API response wrapper for a single team preference override."""
    data: Optional[NotificationTeamPreferenceResp] = None
