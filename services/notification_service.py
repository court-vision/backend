"""
Notification Service

Handles sending lineup alert notifications to users.
Currently uses a stub email sender; will integrate with Resend when configured.
"""

import json
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger
from core.settings import settings


@dataclass
class NotificationResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class NotificationService:
    """Sends lineup alert notifications via email."""

    def __init__(self):
        self.log = get_logger("notification_service")
        self.resend_api_key = getattr(settings, "resend_api_key", None)

    def send_lineup_alert(
        self,
        user,
        team,
        issues: list,
        first_game_time,
        prefs=None,
    ) -> NotificationResult:
        """
        Send a lineup alert email to the user.

        Args:
            user: User model instance (has .email)
            team: Team model instance (has .league_info JSON string)
            issues: List of LineupIssue objects
            first_game_time: datetime.time of the first game today (ET)
            prefs: Optional NotificationPreference model instance

        Returns:
            NotificationResult with success status
        """
        email = prefs.email if prefs and prefs.email else user.email
        subject = f"Court Vision: {len(issues)} lineup issue(s) before today's games"
        body = self._build_alert_body(issues, first_game_time, team)

        self.log.info(
            "sending_lineup_alert",
            to=email,
            issue_count=len(issues),
            subject=subject,
        )

        return self._send_email(email, subject, body)

    def _build_alert_body(self, issues: list, first_game_time, team) -> str:
        """
        Build the plain-text email body for a lineup alert.

        Args:
            issues: List of LineupIssue objects
            first_game_time: datetime.time of the first game today (ET)
            team: Team model instance

        Returns:
            Formatted email body string
        """
        # Parse team name from league_info JSON
        try:
            league_info = json.loads(team.league_info)
            team_name = league_info.get("team_name", "Your Team")
        except (json.JSONDecodeError, AttributeError):
            team_name = "Your Team"

        # Format game time
        if first_game_time:
            game_time_str = first_game_time.strftime("%I:%M %p ET")
        else:
            game_time_str = "TBD"

        lines = [
            f"Team: {team_name}",
            f"First game today: {game_time_str}",
            "",
            f"Found {len(issues)} lineup issue(s):",
            "",
        ]

        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue.suggested_action}")

        lines.append("")
        lines.append("-- Court Vision")

        return "\n".join(lines)

    def _send_email(self, to: str, subject: str, body: str) -> NotificationResult:
        """
        Send an email via Resend. Falls back to log stub if no API key configured.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (plain text)

        Returns:
            NotificationResult
        """
        if self.resend_api_key:
            try:
                import resend

                resend.api_key = self.resend_api_key.get_secret_value()
                result = resend.Emails.send({
                    "from": f"Court Vision <{settings.notification_from_email}>",
                    "to": [to],
                    "subject": subject,
                    "text": body,
                })
                message_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
                self.log.info("email_sent", to=to, message_id=message_id)
                return NotificationResult(success=True, message_id=message_id)
            except Exception as e:
                self.log.error("email_send_failed", to=to, error=str(e))
                return NotificationResult(success=False, error=str(e))

        # Stub: log the email and return success
        self.log.info(
            "email_stub",
            to=to,
            subject=subject,
            body_preview=body[:200],
        )
        return NotificationResult(success=True, message_id=f"stub-{to}")
