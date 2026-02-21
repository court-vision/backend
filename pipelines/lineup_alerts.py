"""
Lineup Alerts Pipeline

Checks all eligible users' lineups and sends notifications if issues are found.
Self-gates in two stages:
  1. Broad gate: only enters the user loop when within the configured outer window
     (LINEUP_ALERT_WINDOW_MINUTES env var, default 150 min) before first tip-off.
  2. Per-user gate: each team is only alerted when within that team's configured
     alert_minutes_before window.

Safe to call frequently (every 15 min); deduplication prevents repeat notifications.
"""

import json
import time as time_mod
from datetime import datetime, timedelta, time

import pytz

from core.logging import get_logger
from core.settings import settings
from db.models.users import User
from db.models.teams import Team
from db.models.nba.games import Game
from db.models.notifications import NotificationPreference, NotificationLog, NotificationTeamPreference
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import ESPNExtractor
from services.lineup_check_service import LineupCheckService
from services.notification_service import NotificationService


class LineupAlertsPipeline(BasePipeline):
    """
    Check user lineups and send alerts before games start.

    This pipeline:
    1. Checks if we're within the notification window (before first game)
    2. Fetches all eligible users with ESPN teams
    3. For each team, checks for lineup issues
    4. Sends notifications for teams with issues
    5. Logs all results for dedup and auditing
    """

    config = PipelineConfig(
        name="lineup_alerts",
        display_name="Lineup Alerts",
        description="Checks user lineups and sends alerts before games start",
        target_table="usr.notification_log",
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()
        self.lineup_checker = LineupCheckService()
        self.notification_service = NotificationService()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the lineup alerts pipeline."""
        eastern = pytz.timezone("US/Eastern")
        now_et = datetime.now(eastern)
        today = now_et.date()

        # Step 1: Check if there are games today with start times
        earliest_game_time = Game.get_earliest_game_time_on_date(today)
        if not earliest_game_time:
            ctx.log.info("no_games_today", date=str(today))
            return

        # Step 2: Check if we're in the notification window
        now_et_time = now_et.time()
        if not self._in_notification_window(now_et_time, earliest_game_time):
            ctx.log.info(
                "outside_notification_window",
                current_time=str(now_et_time),
                first_game=str(earliest_game_time),
            )
            return

        ctx.log.info(
            "notification_window_active",
            first_game=str(earliest_game_time),
            current_time=str(now_et_time),
        )

        # Step 3: Get teams playing today
        teams_playing = Game.get_teams_playing_on_date(today)
        if not teams_playing:
            ctx.log.info("no_teams_playing", date=str(today))
            return

        # Step 4: Get all eligible users
        users_with_prefs = self._get_eligible_users()
        ctx.log.info("eligible_users", count=len(users_with_prefs))

        # Step 5: Process each user's teams
        for user, prefs in users_with_prefs:
            teams = list(Team.select().where(Team.user_id == user.user_id))

            for team in teams:
                try:
                    self._process_team(ctx, user, team, prefs, teams_playing, today, now_et_time, earliest_game_time)
                    # Rate limit ESPN API calls
                    time_mod.sleep(1)
                except Exception as e:
                    ctx.log.warning(
                        "team_alert_error",
                        user_id=user.user_id,
                        team_id=team.team_id,
                        error=str(e),
                    )
                    continue

    def _process_team(
        self,
        ctx: PipelineContext,
        user: User,
        team: Team,
        prefs,
        teams_playing: set[str],
        today,
        now_et_time: time,
        earliest_game_time,
    ) -> None:
        """Process a single team for lineup alerts."""
        # Parse league info
        league_info = json.loads(team.league_info)
        provider = league_info.get("provider", "espn")

        # ESPN only for now
        if provider != "espn":
            return

        # Look up team-level override and merge with global prefs
        team_pref = (
            NotificationTeamPreference.select()
            .where(
                (NotificationTeamPreference.user == user.user_id)
                & (NotificationTeamPreference.team_id == team.team_id)
            )
            .first()
        )
        effective_prefs = self._get_effective_prefs(prefs, team_pref)

        # If team-level alerts are explicitly disabled, skip this team
        if effective_prefs.lineup_alerts_enabled is False:
            ctx.log.debug("team_alerts_disabled", user_id=user.user_id, team_id=team.team_id)
            return

        # Check if we're within this team's configured alert window
        game_dt = datetime.combine(today, earliest_game_time)
        now_dt = datetime.combine(today, now_et_time)
        user_window_start = game_dt - timedelta(minutes=effective_prefs.alert_minutes_before)
        user_window_end = game_dt - timedelta(minutes=15)
        if not (user_window_start <= now_dt <= user_window_end):
            ctx.log.debug(
                "outside_user_notification_window",
                user_id=user.user_id,
                team_id=team.team_id,
                alert_minutes_before=effective_prefs.alert_minutes_before,
            )
            return

        # Check dedup - already notified today?
        already_sent = (
            NotificationLog.select()
            .where(
                (NotificationLog.user == user.user_id)
                & (NotificationLog.team_id == team.team_id)
                & (NotificationLog.notification_type == "lineup_alert")
                & (NotificationLog.notification_date == today)
            )
            .exists()
        )
        if already_sent:
            ctx.log.debug(
                "already_notified",
                user_id=user.user_id,
                team_id=team.team_id,
            )
            return

        # Fetch roster from ESPN
        roster = self.espn_extractor.get_roster_with_slots(
            league_id=league_info["league_id"],
            team_name=league_info.get("team_name", ""),
            espn_s2=league_info.get("espn_s2", ""),
            swid=league_info.get("swid", ""),
            year=league_info.get("year", settings.espn_year),
        )

        if not roster:
            ctx.log.debug(
                "roster_fetch_failed",
                user_id=user.user_id,
                team_id=team.team_id,
            )
            return

        # Check for lineup issues
        issues = self.lineup_checker.check_lineup(
            roster=roster,
            teams_playing_today=teams_playing,
            prefs=effective_prefs,
        )

        if not issues:
            # No issues - log as skipped so we don't recheck
            self._create_log(user, team, today, status="skipped")
            ctx.log.debug(
                "no_issues",
                user_id=user.user_id,
                team_id=team.team_id,
            )
            return

        # Send notification
        result = self.notification_service.send_lineup_alert(
            user=user,
            team=team,
            issues=issues,
            first_game_time=earliest_game_time,
            prefs=effective_prefs,
        )

        # Log the notification
        alert_data = json.dumps([
            {
                "issue_type": issue.issue_type.value,
                "player_name": issue.player_name,
                "player_team": issue.player_team,
                "current_slot": issue.current_slot,
                "suggested_action": issue.suggested_action,
            }
            for issue in issues
        ])

        self._create_log(
            user=user,
            team=team,
            today=today,
            status="sent" if result.success else "failed",
            alert_data=alert_data,
            resend_message_id=result.message_id,
            error_message=result.error,
        )

        ctx.increment_records()
        ctx.log.info(
            "alert_sent",
            user_id=user.user_id,
            team_id=team.team_id,
            issue_count=len(issues),
            success=result.success,
        )

    def _get_effective_prefs(self, global_prefs, team_pref):
        """Merge team override on top of global prefs. Team wins where not None."""
        if team_pref is None:
            return global_prefs

        class _EffectivePrefs:
            pass

        ep = _EffectivePrefs()
        for field in [
            "lineup_alerts_enabled", "alert_benched_starters",
            "alert_active_non_playing", "alert_injured_active",
            "alert_minutes_before", "email",
        ]:
            team_val = getattr(team_pref, field, None)
            setattr(ep, field, team_val if team_val is not None else getattr(global_prefs, field))
        return ep

    def _in_notification_window(
        self,
        now_et: time,
        first_game_time: time,
    ) -> bool:
        """
        Check if current time is within the notification window.

        Window is [first_game - alert_window, first_game - 15 min].
        """
        window_minutes = settings.lineup_alert_window_minutes
        today = datetime.today().date()

        game_dt = datetime.combine(today, first_game_time)
        now_dt = datetime.combine(today, now_et)

        window_start = game_dt - timedelta(minutes=window_minutes)
        window_end = game_dt - timedelta(minutes=15)

        return window_start <= now_dt <= window_end

    def _get_eligible_users(self) -> list[tuple]:
        """
        Get users eligible for lineup alerts.

        Opt-in model: only processes users who have an explicit
        NotificationPreference row with lineup_alerts_enabled=True.
        Users without a row are not processed until they opt in via the UI.

        Returns list of (User, NotificationPreference) tuples.
        """
        enabled_prefs = list(
            NotificationPreference.select()
            .where(NotificationPreference.lineup_alerts_enabled == True)
        )

        result = []
        for pref in enabled_prefs:
            user = User.select().where(User.user_id == pref.user_id).first()
            if user:
                result.append((user, pref))

        return result

    def _create_log(
        self,
        user: User,
        team: Team,
        today,
        status: str,
        alert_data: str | None = None,
        resend_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Create a NotificationLog entry."""
        NotificationLog.create(
            user=user.user_id,
            team_id=team.team_id,
            notification_type="lineup_alert",
            notification_date=today,
            alert_data=alert_data,
            status=status,
            resend_message_id=resend_message_id,
            error_message=error_message,
            sent_at=datetime.utcnow() if status == "sent" else None,
        )
