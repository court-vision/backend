"""
Lineup Check Service

Pure logic for detecting lineup issues in fantasy basketball rosters.
No I/O - takes roster data and returns issues found.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LineupIssueType(str, Enum):
    BENCHED_STARTER = "benched_starter"
    ACTIVE_NOT_PLAYING = "active_not_playing"
    INJURED_ACTIVE = "injured_active"


@dataclass
class LineupIssue:
    issue_type: LineupIssueType
    player_name: str
    player_team: str
    current_slot: str
    suggested_action: str
    injury_status: Optional[str] = None


ACTIVE_SLOTS = {"PG", "SG", "SF", "PF", "C", "G", "F", "SG/SF", "G/F", "PF/C", "F/C", "UT"}
BENCH_SLOTS = {"BE"}
IR_SLOTS = {"IR"}


class LineupCheckService:
    """Checks a fantasy roster for lineup issues."""

    def check_lineup(
        self,
        roster: list[dict],
        teams_playing_today: set[str],
        prefs=None,
    ) -> list[LineupIssue]:
        """
        Check a roster for lineup issues.

        Args:
            roster: List of player dicts with keys: name, team, lineup_slot, injured, injury_status
            teams_playing_today: Set of team abbreviation strings for teams with games today
            prefs: Optional NotificationPreference model instance

        Returns:
            List of LineupIssue objects describing problems found
        """
        issues: list[LineupIssue] = []

        alert_benched = getattr(prefs, "alert_benched_starters", True) if prefs else True
        alert_active_non_playing = getattr(prefs, "alert_active_non_playing", True) if prefs else True
        alert_injured = getattr(prefs, "alert_injured_active", True) if prefs else True

        for player in roster:
            name = player.get("name", "Unknown")
            team = player.get("team", "")
            slot = player.get("lineup_slot", "")
            injured = player.get("injured", False)
            injury_status = player.get("injury_status")

            # BENCHED_STARTER: player on bench, team plays today, NOT injured
            if alert_benched and slot in BENCH_SLOTS:
                if team in teams_playing_today and not injured:
                    issues.append(LineupIssue(
                        issue_type=LineupIssueType.BENCHED_STARTER,
                        player_name=name,
                        player_team=team,
                        current_slot=slot,
                        suggested_action=f"Move {name} ({team}) from bench to an active slot",
                    ))

            # ACTIVE_NOT_PLAYING: player in active slot, team does NOT play today, NOT injured
            # Exclude free agents (team == "FA")
            if alert_active_non_playing and slot in ACTIVE_SLOTS:
                if team != "FA" and team not in teams_playing_today and not injured:
                    issues.append(LineupIssue(
                        issue_type=LineupIssueType.ACTIVE_NOT_PLAYING,
                        player_name=name,
                        player_team=team,
                        current_slot=slot,
                        suggested_action=f"Consider benching {name} ({team}) - no game today",
                    ))

            # INJURED_ACTIVE: player in active slot AND injured
            if alert_injured and slot in ACTIVE_SLOTS:
                if injured:
                    issues.append(LineupIssue(
                        issue_type=LineupIssueType.INJURED_ACTIVE,
                        player_name=name,
                        player_team=team,
                        current_slot=slot,
                        suggested_action=f"Move {name} ({team}) to IR or bench - status: {injury_status or 'injured'}",
                        injury_status=injury_status,
                    ))

        return issues
