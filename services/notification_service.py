"""
Notification Service

Emails users about today's lineup: either the fill plan the backend suggested
(`send_lineup_alert`) or the moves auto-lineup already applied to ESPN
(`send_auto_lineup_summary`) — what became of a scheduled pickup
(`send_scheduled_pickup_result`, a stream lane's included), a stream lane
that paused or missed a day (`send_stream_lane_notice`), and the late-scratch
swap a verdict change made or suggests (`send_late_scratch_result`), and what
Daily actions at Act made (`send_daily_actions_result`: IR moves and open-spot
pickups). Sends through Resend when configured; otherwise logs the email and
reports success (the stub path for dev and tests).

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
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core.logging import get_logger
from core.settings import settings

AUTO_LINEUP_DISCLAIMER = (
    "Auto-lineup only fills empty or idle slots with players who have a game today; "
    "it never benches a healthy starter and never touches IR."
)
UNVERIFIED_SUFFIX = " (not yet confirmed by ESPN — please check your roster)"
SIGNATURE = "-- Court Vision"
# Who decided a verdict (nba.player_availability.decided_by), as the email names it
VERDICT_SOURCES = {
    "nba_report": "the NBA injury report",
    "nba_boxscore": "the NBA box score",
    "espn_injuries": "ESPN's injury list",
    "espn_fantasy": "ESPN fantasy",
    "news": "the news",
}


@dataclass
class NotificationResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class NotificationService:
    """Sends lineup alert, auto-lineup summary, scheduled-pickup result and stream lane emails."""

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

    def send_scheduled_pickup_result(
        self,
        user,
        team,
        result: dict,
        prefs=None,
    ) -> NotificationResult:
        """
        Email the user what became of a scheduled pickup once the executor settled it.

        Args:
            result: The backend's PickupResult as a dict — `outcome` (executed |
                skipped | failed | expired), `reason`, `detail`, `add` / `drop`
                ({name, team}), `nba_date` (ISO), `seated_slot`, `verified`.
        """
        email = self._recipient(user, prefs)
        team_name = self._team_name(team)
        subject, body = self._build_pickup_email(team_name, result)

        self.log.info(
            "sending_scheduled_pickup_result",
            to=email,
            outcome=result.get("outcome"),
            reason=result.get("reason"),
            subject=subject,
        )

        return self._send_email(email, subject, body)

    def send_stream_lane_notice(
        self,
        user,
        team,
        notice: dict,
        prefs=None,
    ) -> NotificationResult:
        """
        Email the user that their stream lane could not act by itself.

        Args:
            notice: The backend's LaneNotice as a dict — `action` (paused | blocked),
                `reason`, `detail`, `holder` ({name}), `nba_date` (ISO), `ended` (the
                lane ended with it).
        """
        email = self._recipient(user, prefs)
        subject, body = self._build_lane_notice_email(self._team_name(team), notice)

        self.log.info(
            "sending_stream_lane_notice",
            to=email,
            action=notice.get("action"),
            reason=notice.get("reason"),
            subject=subject,
        )

        return self._send_email(email, subject, body)

    def send_late_scratch_result(
        self,
        user,
        team,
        result: dict,
        prefs=None,
    ) -> NotificationResult:
        """
        Email the user the swap a late scratch made on their ESPN lineup, or suggests.

        Args:
            result: The backend's LineupReactTeam as a dict plus the change it answers:
                `outcome` (applied, or planned / rejected / failed: a suggestion),
                `level` (notify | act), `reason`, `player_name`, `verdict` (out |
                doubtful | available), `injury` (the reason, "Left Ankle; Sprain"),
                `source` (decided_by), `changed_at` (ISO), `moves`, `verified`.
        """
        email = self._recipient(user, prefs)
        subject, body = self._build_late_scratch_email(self._team_name(team), result)

        self.log.info(
            "sending_late_scratch_result",
            to=email,
            outcome=result.get("outcome"),
            verdict=result.get("verdict"),
            move_count=len(result.get("moves") or []),
            subject=subject,
        )

        return self._send_email(email, subject, body)

    def send_daily_actions_result(
        self,
        user,
        team,
        result: dict,
        prefs=None,
    ) -> NotificationResult:
        """
        Email the user what Daily actions at Act made on their ESPN roster before today's games.

        Args:
            result: The backend's DailyActionsRunData as a dict: `outcome` (applied, or
                rejected: nothing went), `reason`, `ir_moves` (fill-plan-shaped moves to or
                off IR), `adds` (each `player` {name, team}, `detail`, `verified`), `held`
                (why a spot was left open or a later move didn't go), `open_spots`,
                `first_game_time_et`.
        """
        email = self._recipient(user, prefs)
        subject, body = self._build_daily_actions_email(self._team_name(team), result)

        self.log.info(
            "sending_daily_actions_result",
            to=email,
            outcome=result.get("outcome"),
            ir_moves=len(result.get("ir_moves") or []),
            adds=len(result.get("adds") or []),
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

    @staticmethod
    def _build_pickup_email(team_name: str, result: dict) -> tuple[str, str]:
        """Subject and body for one settled scheduled pickup."""
        add = (result.get("add") or {}).get("name") or "the player"
        drop = (result.get("drop") or {}).get("name")
        day = NotificationService._format_day(result.get("nba_date"))
        outcome = result.get("outcome")
        reason = result.get("reason") or ""
        detail = result.get("detail")
        lines = [f"Team: {team_name}", f"Pickup for: {day}", ""]

        if outcome == "executed":
            subject = f"Court Vision picked up {add} for {team_name}"
            made = f"Added {add}" + (f", dropped {drop}" if drop and reason not in ("drop_missing", "drop_locked",
                                                                                 "drop_protected") else "")
            made += " on ESPN."
            if result.get("verified") is False:
                made += UNVERIFIED_SUFFIX
            lines.append(made)
            seat = result.get("seated_slot")
            if seat:
                lines.append(f"He is in your {seat} slot for {day}.")
            else:
                lines.append(f"He is on your bench for {day} — set your lineup.")
            if reason == "drop_missing" and drop:
                lines.append(f"{drop} was no longer on your roster, so this was an add only.")
            elif reason == "drop_locked" and drop:
                lines.append(f"{drop} was locked for the day, so this was an add only.")
            elif reason == "drop_protected" and drop:
                # The roster lock (backend services/roster_protection.py): protected after scheduling
                lines.append(f"{drop} is protected, so this was an add only.")
        elif outcome == "skipped":
            subject = f"Pickup skipped: {add} for {team_name}"
            why = {
                "unavailable": f"{add} is no longer available in your league.",
                "already_on_roster": f"{add} is already on your roster.",
                "drop_missing": f"{drop} is no longer on your roster and there was no open seat.",
                "drop_locked": f"{drop} was locked for the day and there was no open seat.",
                # A lane's switch is skipped whatever the seats (its add alone would leave the spot to no one)
                "drop_protected": (f"{drop} is protected." if result.get("lane")
                                   else f"{drop} is protected and there was no open seat."),
                "roster_full": "your roster is full.",
                "acquisition_limit": "you had used every add your league allows for it.",
            }.get(reason, (reason or "nothing to do") + ".")
            lines.append(f"The pickup was not made: {why}")
            lines.append("Nothing was changed on ESPN.")
        elif outcome == "expired":
            subject = f"Pickup missed: {add} for {team_name}"
            why = {
                "locked_on_day": f"{add} was locked by the time the move was possible.",
                "deadline": "the day's games started before the move was possible.",
                "day_passed": "the day had already passed.",
            }.get(reason, (reason or "the window closed") + ".")
            lines.append(f"The pickup could not be made in time: {why}")
            lines.append("Nothing was changed on ESPN.")
        else:
            subject = f"Pickup failed: {add} for {team_name}"
            why = {
                "espn_rejected": "ESPN refused the transaction.",
                "auth_expired": "your ESPN connection has expired — reconnect it on Court Vision.",
                "max_attempts": "it was retried too many times without going through.",
                "team_not_found": "the team is no longer on Court Vision.",
            }.get(reason, (reason or "an unexpected error") + ".")
            lines.append(f"The pickup failed: {why}")
            if detail and reason == "espn_rejected":
                lines.append(f"ESPN said: {detail}")
            lines.append("Nothing was changed on ESPN.")

        lane = result.get("lane")
        if lane:
            subject = {
                "executed": f"Stream lane picked up {add} for {team_name}",
                "skipped": f"Stream lane skipped {add} for {team_name}",
                "expired": f"Stream lane missed {add} for {team_name}",
            }.get(outcome, f"Stream lane pickup failed: {add} for {team_name}")
            lines.extend(NotificationService._lane_pickup_lines(lane, outcome, add, drop, reason=reason))

        lines.append("")
        lines.append(SIGNATURE)
        return subject, "\n".join(lines)

    @staticmethod
    def _lane_pickup_lines(lane: dict, outcome, add: str, drop, *, reason: str = "") -> list[str]:
        """What a stream lane's pickup adds: that the lane made it (and ends with it, on the matchup's
        last day), and how it compares with the projection shown before it (which can change as the
        market does). A switch skipped because the spot's player is protected ends the lane."""
        ends = "for the matchup's last day: the lane ends with the matchup." if lane.get("last_day") else None
        if outcome != "executed" and reason == "drop_protected":
            return ["", "This was your stream lane's pickup. You protected the spot's player, so the lane has ended."]
        if outcome != "executed":
            return ["", f"This was your stream lane's pickup {ends}" if ends
                    else "This was your stream lane's pickup. The lane carries on and re-plans before the next day."]
        lines = ["", f"Your stream lane made this move {ends}" if ends
                 else "Your stream lane made this move: it re-plans the spot before each day."]
        projected = (lane.get("projected") or {}).get("name")
        why = lane.get("why")
        if projected:
            if why == "as_projected":
                lines.append("This was the move the lane had projected.")
            elif why == "projected_gone":
                lines.append(f"The lane had projected {projected} for this day, but {projected} was no longer "
                             f"available, so it picked {add}.")
            elif why == "replanned" and projected == drop:
                lines.append(f"The lane had projected keeping {drop} for this day; with the latest projections "
                             f"{add} was the better pick.")
            elif why == "replanned":
                lines.append(f"The lane had projected {projected} for this day; with the latest projections "
                             f"{add} was the better pick.")
        return lines

    @staticmethod
    def _build_lane_notice_email(team_name: str, notice: dict) -> tuple[str, str]:
        """Subject and body for a stream lane that paused, or a day it missed."""
        holder = (notice.get("holder") or {}).get("name") or "the spot's player"
        reason = notice.get("reason") or ""
        lines = [f"Team: {team_name}", f"Stream lane on: {holder}'s spot", ""]
        if notice.get("action") == "paused":
            subject = f"Stream lane paused for {team_name}"
            why = {
                "holder_gone": f"{holder} is no longer on your roster, so the lane has no spot to stream. "
                               "Start a new lane from a seat on Court Vision.",
                "auth_expired": "your ESPN connection has expired — reconnect it on Court Vision, then resume the lane.",
                "provider_not_supported": "your league's rosters can't be changed from Court Vision.",
                "waiver_period_unsupported": "your league's waiver period is longer than stream lanes support.",
            }.get(reason, f"{reason or 'an unexpected problem'}. Resume it on Court Vision when it is fixed.")
            lines.append(f"The lane paused: {why}")
            lines.append("The lane won't change anything else on ESPN." if reason == "holder_gone"
                         else "Nothing more will be changed on ESPN until you resume it.")
        else:
            day = NotificationService._format_day(notice.get("nba_date"))
            subject = f"Stream lane missed a day for {team_name}"
            lines.append(f"No decision was made in time for {day}, so the spot stayed as it was.")
            lines.append("The matchup has no day left, so the lane has ended." if notice.get("ended")
                         else "The lane carries on with the next day.")
        lines.append("")
        lines.append(SIGNATURE)
        return subject, "\n".join(lines)

    @staticmethod
    def _build_late_scratch_email(team_name: str, result: dict) -> tuple[str, str]:
        """Subject and body for one late-scratch swap: made (applied) or suggested (planned)."""
        player = result.get("player_name") or "a player"
        verdict = result.get("verdict") or "out"
        applied = result.get("outcome") == "applied"
        # The write raised after it was sent: ESPN may have taken it.
        uncertain = result.get("outcome") == "failed" and result.get("reason") == "write_uncertain"
        because = (f"{player} was upgraded to available" if verdict == "available"
                   else f"{player} was ruled {verdict}")
        injury = result.get("injury")
        source = VERDICT_SOURCES.get(result.get("source") or "", result.get("source"))
        why = ", ".join(x for x in (injury, f"per {source}" if source else None) if x)
        when = NotificationService._format_clock(result.get("changed_at"))
        line = f"Because {because}" + (f" ({why})" if why else "") + (f" at {when}" if when else "") + ":"

        if verdict == "available":
            subject = (f"{player} is available: Court Vision started him for {team_name}" if applied
                       else f"{player} is available: a lineup swap for {team_name}")
        else:
            subject = (f"{player} ruled {verdict}: Court Vision benched him for {team_name}" if applied
                       else f"{player} ruled {verdict}: a lineup swap for {team_name}")

        lines = [f"Team: {team_name}", "", line, ""]
        if applied:
            heading = "Applied to ESPN:"
            if result.get("verified") is False:
                heading += UNVERIFIED_SUFFIX
            lines.append(heading)
        else:
            lines.append("Tried on ESPN, not confirmed:" if uncertain else "Suggested swap:")
        lines.extend(NotificationService._render_moves(result.get("moves") or []))
        lines.append("")
        if applied:
            lines.append("Only this swap was made; the rest of your lineup is as you set it.")
            if verdict != "available":
                lines.append(f"If {player} is upgraded again before his game, this swap is not undone.")
        else:
            why_not = NotificationService._late_scratch_not_made(result)
            if why_not:
                lines.append(why_not)
            lines.append("If it isn't on ESPN, make it in Court Vision's Week desk or on ESPN." if uncertain
                         else "Nothing was changed on ESPN. Make the swap in Court Vision's Week desk or on ESPN.")
        lines.append("")
        lines.append(SIGNATURE)
        return subject, "\n".join(lines)

    @staticmethod
    def _build_daily_actions_email(team_name: str, result: dict) -> tuple[str, str]:
        """Subject and body for one team's daily actions at Act: what was made (applied) or why
        nothing was (rejected)."""
        ir_moves = result.get("ir_moves") or []
        adds = result.get("adds") or []
        did: list[str] = []
        lines_made: list[str] = []
        for move in ir_moves:
            name = move.get("name") or f"player {move.get('player_id', '?')}"
            note = f" ({move['note']})" if move.get("note") else ""
            if move.get("to_slot") == "IR":
                did.append(f"moved {name} to IR")
                lines_made.append(f"Moved {name} to IR{note}")
            else:
                did.append(f"moved {name} off IR")
                lines_made.append(f"Moved {name} off IR to {move.get('to_slot') or 'the bench'}{note}")
        for add in adds:
            player = add.get("player") or {}
            name = player.get("name") or "a free agent"
            did.append(f"picked up {name}")
            team = f" ({player['team']})" if player.get("team") else ""
            line = f"Picked up {name}{team} for an open roster spot"
            if add.get("verified") is False:
                line += UNVERIFIED_SUFFIX
            lines_made.append(line)
            if add.get("detail"):
                lines_made.append(f"   {add['detail']}")

        lines = [f"Team: {team_name}"]
        tip = result.get("first_game_time_et")
        if tip:
            try:
                from datetime import time as _time
                tip = _time.fromisoformat(str(tip)).strftime("%-I:%M %p ET")
            except ValueError:
                pass
            lines.append(f"First game today: {tip}")
        lines.append("")
        if result.get("outcome") == "applied" and did:
            what = did[0] if len(did) == 1 else ", ".join(did[:-1]) + " and " + did[-1]
            subject = f"Court Vision {what} for {team_name}"
            lines.append("Your Daily actions are at Act, so before today's games Court Vision made these on ESPN:")
            lines.extend(f"  {line}" if line.startswith("   ") else f"  - {line}" for line in lines_made)
        else:
            reason = (result.get("reason") or "").removeprefix("PROVIDER_AUTH_EXPIRED: ")
            subject = f"Court Vision couldn't make today's daily actions for {team_name}"
            lines.append("Your Daily actions are at Act, but ESPN didn't take the first move, so nothing was changed"
                         + (f": {reason}" if reason else "."))
        if result.get("held"):
            lines.append("")
            lines.append(f"Not made: {result['held']}.".replace("..", "."))
        lines.append("")
        lines.append("Nobody was dropped: the day's drop-and-add swap is yours to make in Court Vision's Week desk.")
        if adds:
            lines.append("A pickup joins your bench; your Lineup automation decides whether he starts tonight.")
        lines.append("")
        lines.append(SIGNATURE)
        return subject, "\n".join(lines)

    @staticmethod
    def _late_scratch_not_made(result: dict) -> Optional[str]:
        """Why a swap the team's Act level asked for wasn't made; None at Notify."""
        if result.get("level") != "act":
            return None
        outcome = result.get("outcome")
        reason = result.get("reason") or ""
        if outcome == "planned" and reason == "writes_disabled":
            return "Court Vision didn't make it: its lineup writes are switched off right now."
        if outcome == "planned" and reason.startswith("can_write:"):
            return "Court Vision didn't make it: ESPN isn't taking lineup changes for this team right now."
        if outcome == "failed" and reason == "write_uncertain":
            return "Court Vision tried to make it but couldn't confirm whether ESPN took it: check your lineup on ESPN."
        if outcome in ("rejected", "failed"):
            return "Court Vision tried to make it, but ESPN didn't take it" + (f" ({reason})." if reason else ".")
        return None

    @staticmethod
    def _format_clock(iso_time) -> Optional[str]:
        """\"4:30 PM ET\" from an ISO timestamp (UTC when it has no zone); None when it is not one."""
        if not iso_time:
            return None
        try:
            at = iso_time if isinstance(iso_time, datetime) else datetime.fromisoformat(str(iso_time))
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=ZoneInfo("UTC"))
        return at.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")

    @staticmethod
    def _format_day(iso_date) -> str:
        """\"Thu, Nov 12\" from an ISO date; the raw value when it is not one."""
        if not iso_date:
            return "that day"
        try:
            from datetime import date as _date
            d = _date.fromisoformat(str(iso_date))
        except ValueError:
            return str(iso_date)
        return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"

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
