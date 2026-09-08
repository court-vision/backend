"""
Notification Service

Emails users about today's lineup: either the fill plan the backend suggested
(`send_lineup_alert`) or the moves auto-lineup already applied to ESPN
(`send_auto_lineup_summary`). Sends through Resend when configured; otherwise
logs the email and reports success (the stub path for dev and tests).

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py) — keep it free of service-specific imports.

`moves` are the backend's fill-plan entries: dicts with `player_id`, `name`,
`from_slot_id`, `from_slot`, `to_slot_id`, `to_slot`, `role` (`start` |
`bench` | `shift`) and an optional `note` ("vs LAL · 7:30 PM" on a start,
"no game today" / "OUT" on a bench). `unfilled` entries carry `name`, `slot`
and `reason`.
"""

import json
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger
from core.settings import settings

AUTO_LINEUP_DISCLAIMER = (
    "Auto-lineup only fills empty or idle slots with players who have a game today; "
    "it never benches a healthy starter and never touches IR."
)
UNVERIFIED_SUFFIX = " (not yet confirmed by ESPN — please check your roster)"
SIGNATURE = "-- Court Vision"


@dataclass
class NotificationResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class NotificationService:
    """Sends lineup alert and auto-lineup summary emails."""

    def __init__(self):
        self.log = get_logger("notification_service")
        self.resend_api_key = getattr(settings, "resend_api_key", None)
        if not self.resend_api_key:
            self.log.warning("resend_api_key_not_configured_emails_will_not_be_sent")

    # ---- public API -------------------------------------------------------------

    def send_lineup_alert(
        self,
        user,
        team,
        moves: list[dict],
        unfilled: list[dict],
        first_game_time,
        prefs=None,
        extra_lines: Optional[list[str]] = None,
    ) -> NotificationResult:
        """
        Email the user the lineup moves the backend suggests for today.

        Args:
            user: User model instance (has .email)
            team: Team model instance (has .league_info JSON string)
            moves: Fill-plan moves (see module docstring)
            unfilled: Bench players with no eligible open slot
            first_game_time: datetime.time (ET) of today's first game, or a preformatted string
            prefs: Optional NotificationPreference; its .email overrides user.email
            extra_lines: Appended before the signature (e.g. why auto-lineup could not apply)

        Returns:
            NotificationResult with success status
        """
        email = self._recipient(user, prefs)
        subject = f"Court Vision: {len(moves)} suggested lineup move(s) before today's games"
        body = self._build_alert_body(team, moves, unfilled, first_game_time, extra_lines)

        self.log.info(
            "sending_lineup_alert",
            to=email,
            move_count=len(moves),
            unfilled_count=len(unfilled),
            subject=subject,
        )

        return self._send_email(email, subject, body)

    def send_auto_lineup_summary(
        self,
        user,
        team,
        moves: list[dict],
        unfilled: list[dict],
        first_game_time,
        prefs=None,
        verified: Optional[bool] = None,
    ) -> NotificationResult:
        """
        Email the user what auto-lineup just applied to their ESPN roster.

        Args:
            verified: Whether the backend's re-read confirmed the moves; False adds a
                "please check your roster" note, None says nothing about it.
        """
        email = self._recipient(user, prefs)
        team_name = self._team_name(team)
        subject = f"Court Vision set your lineup for {team_name}: {len(moves)} move(s)"
        body = self._build_summary_body(team, moves, unfilled, first_game_time, verified)

        self.log.info(
            "sending_auto_lineup_summary",
            to=email,
            move_count=len(moves),
            unfilled_count=len(unfilled),
            verified=verified,
            subject=subject,
        )

        return self._send_email(email, subject, body)

    # ---- body rendering ---------------------------------------------------------

    def _build_alert_body(
        self,
        team,
        moves: list[dict],
        unfilled: list[dict],
        first_game_time,
        extra_lines: Optional[list[str]] = None,
    ) -> str:
        lines = self._header_lines(team, first_game_time)
        lines.append(f"Suggested lineup moves ({len(moves)}):")
        lines.extend(self._render_moves(moves))
        lines.extend(self._render_unfilled(unfilled))
        if extra_lines:
            lines.append("")
            lines.extend(extra_lines)
        lines.append("")
        lines.append(SIGNATURE)
        return "\n".join(lines)

    def _build_summary_body(
        self,
        team,
        moves: list[dict],
        unfilled: list[dict],
        first_game_time,
        verified: Optional[bool],
    ) -> str:
        lines = self._header_lines(team, first_game_time)
        heading = "Applied to ESPN:"
        if verified is False:
            heading += UNVERIFIED_SUFFIX
        lines.append(heading)
        lines.extend(self._render_moves(moves))
        lines.extend(self._render_unfilled(unfilled))
        lines.append("")
        lines.append(AUTO_LINEUP_DISCLAIMER)
        lines.append("")
        lines.append(SIGNATURE)
        return "\n".join(lines)

    def _header_lines(self, team, first_game_time) -> list[str]:
        return [
            f"Team: {self._team_name(team)}",
            f"First game today: {self._format_game_time(first_game_time)}",
            "",
        ]

    @staticmethod
    def _render_moves(moves: list[dict]) -> list[str]:
        """Number each swap: a `start` move followed by the `bench`/`shift` moves it displaces."""
        groups: list[list[dict]] = []
        for move in moves:
            role = move.get("role")
            if role == "start" or not groups:
                groups.append([move])
            else:
                groups[-1].append(move)

        lines: list[str] = []
        for number, group in enumerate(groups, 1):
            first = True
            for move in group:
                prefix = f"  {number}. " if first else "     "
                lines.append(prefix + NotificationService._describe_move(move))
                first = False
        return lines

    @staticmethod
    def _describe_move(move: dict) -> str:
        name = move.get("name") or f"player {move.get('player_id', '?')}"
        role = move.get("role")
        note = move.get("note")
        suffix = f" ({note})" if note else ""
        if role == "start":
            return f"Start {name} at {move.get('to_slot', '?')}{suffix}"
        if role == "bench":
            return f"Bench {name}{suffix}"
        if role == "shift":
            return f"Shift {name} {move.get('from_slot', '?')}→{move.get('to_slot', '?')}{suffix}"
        return f"Move {name} {move.get('from_slot', '?')}→{move.get('to_slot', '?')}{suffix}"

    @staticmethod
    def _render_unfilled(unfilled: list[dict]) -> list[str]:
        if not unfilled:
            return []
        lines = ["", "Still on the bench (no eligible open slot):"]
        for entry in unfilled:
            name = entry.get("name") or f"player {entry.get('player_id', '?')}"
            slot = entry.get("slot") or "BE"
            reason = entry.get("reason") or "no eligible open slot"
            lines.append(f"  - {name} ({slot}): {reason}")
        return lines

    @staticmethod
    def _recipient(user, prefs) -> str:
        return prefs.email if prefs and getattr(prefs, "email", None) else user.email

    @staticmethod
    def _team_name(team) -> str:
        try:
            league_info = json.loads(team.league_info)
            return league_info.get("team_name") or "Your Team"
        except (json.JSONDecodeError, AttributeError, TypeError):
            return "Your Team"

    @staticmethod
    def _format_game_time(first_game_time) -> str:
        if first_game_time is None:
            return "TBD"
        if hasattr(first_game_time, "strftime"):
            return first_game_time.strftime("%I:%M %p ET")
        return str(first_game_time)

    # ---- transport --------------------------------------------------------------

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
