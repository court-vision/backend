"""
Notification Models

Models for user notification preferences and notification logs.
Used by the lineup alerts feature to track preferences and dedup notifications.
"""

import uuid
from datetime import datetime

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    TextField,
    UUIDField,
)

from db.base import BaseModel
from db.models.users import User


class NotificationPreference(BaseModel):
    """
    Per-user notification preferences.

    Controls which lineup alert types are enabled and timing configuration.
    One row per user (unique constraint on user FK).
    """

    id = AutoField(primary_key=True)
    user = ForeignKeyField(User, unique=True, on_delete="CASCADE", backref="notification_prefs")
    lineup_alerts_enabled = BooleanField(default=True)
    alert_benched_starters = BooleanField(default=True)
    alert_active_non_playing = BooleanField(default=True)
    alert_injured_active = BooleanField(default=True)
    alert_minutes_before = IntegerField(default=90)
    email = CharField(max_length=255, null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "notification_preferences"
        schema = "usr"

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    def __repr__(self):
        return (
            f"<NotificationPreference("
            f"user={self.user_id}, "
            f"alerts_enabled={self.lineup_alerts_enabled})>"
        )


class NotificationLog(BaseModel):
    """
    Log of sent (or attempted) notifications.

    Used for deduplication (unique index on user+team+type+date)
    and audit trail of all notification activity.
    """

    id = UUIDField(primary_key=True, default=uuid.uuid4)
    user = ForeignKeyField(User, on_delete="CASCADE", backref="notification_logs")
    team_id = IntegerField()
    notification_type = CharField(max_length=50)
    notification_date = DateField(index=True)
    alert_data = TextField(null=True)  # JSON of issues found
    status = CharField(max_length=20, default="pending")  # pending/sent/failed/skipped
    resend_message_id = CharField(max_length=100, null=True)
    error_message = TextField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    sent_at = DateTimeField(null=True)

    class Meta:
        table_name = "notification_log"
        schema = "usr"
        indexes = (
            (("user", "team_id", "notification_type", "notification_date"), True),
        )

    def __repr__(self):
        return (
            f"<NotificationLog("
            f"user={self.user_id}, "
            f"team={self.team_id}, "
            f"type={self.notification_type}, "
            f"status={self.status})>"
        )


class NotificationTeamPreference(BaseModel):
    """
    Per-team notification preference overrides.

    All preference fields are nullable. None means inherit from the user's
    global NotificationPreference. Only non-None values override the global.
    Unique constraint on (user, team_id).
    """

    id = AutoField(primary_key=True)
    user = ForeignKeyField(User, on_delete="CASCADE", backref="team_notification_prefs")
    team_id = IntegerField()
    # All nullable — None = inherit from global NotificationPreference
    lineup_alerts_enabled = BooleanField(null=True, default=None)
    alert_benched_starters = BooleanField(null=True, default=None)
    alert_active_non_playing = BooleanField(null=True, default=None)
    alert_injured_active = BooleanField(null=True, default=None)
    alert_minutes_before = IntegerField(null=True, default=None)
    email = CharField(max_length=255, null=True, default=None)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "notification_team_preferences"
        schema = "usr"
        indexes = ((("user", "team_id"), True),)

    def save(self, *args, **kwargs):
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    def __repr__(self):
        return f"<NotificationTeamPreference(user={self.user_id}, team={self.team_id})>"
