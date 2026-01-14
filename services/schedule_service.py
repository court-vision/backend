import json
import os
from datetime import datetime, date
from typing import Optional
from pathlib import Path


# Load schedule data at module level
_SCHEDULE_DATA: dict = {}

def _load_schedule() -> dict:
    """Load the schedule JSON file."""
    global _SCHEDULE_DATA
    if not _SCHEDULE_DATA:
        schedule_path = Path(__file__).parent.parent / "static" / "schedule25-26.json"
        with open(schedule_path, "r") as f:
            _SCHEDULE_DATA = json.load(f)
    return _SCHEDULE_DATA


def _parse_date(date_str: str) -> date:
    """Parse date string in MM/DD/YYYY format."""
    return datetime.strptime(date_str, "%m/%d/%Y").date()


def get_current_matchup(current_date: Optional[date] = None) -> Optional[dict]:
    """
    Get the current matchup info based on the provided date.

    Args:
        current_date: The date to check. Defaults to today.

    Returns:
        Dict with matchup info including 'matchup_number', 'start_date', 'end_date',
        'game_span', 'games', and 'current_day_index', or None if no matchup found.
    """
    if current_date is None:
        current_date = date.today()

    schedule = _load_schedule().get("schedule", {})

    for matchup_num, matchup_data in schedule.items():
        start_date = _parse_date(matchup_data["startDate"])
        end_date = _parse_date(matchup_data["endDate"])

        if start_date <= current_date <= end_date:
            day_index = (current_date - start_date).days
            return {
                "matchup_number": int(matchup_num),
                "start_date": start_date,
                "end_date": end_date,
                "game_span": matchup_data["gameSpan"],
                "games": matchup_data["games"],
                "current_day_index": day_index
            }

    return None


def get_matchup_by_number(matchup_number: int) -> Optional[dict]:
    """
    Get matchup info by matchup number.

    Args:
        matchup_number: The matchup number (1-20 for 2025-26 season).

    Returns:
        Dict with matchup info or None if not found.
    """
    schedule = _load_schedule().get("schedule", {})
    matchup_data = schedule.get(str(matchup_number))

    if matchup_data:
        return {
            "matchup_number": matchup_number,
            "start_date": _parse_date(matchup_data["startDate"]),
            "end_date": _parse_date(matchup_data["endDate"]),
            "game_span": matchup_data["gameSpan"],
            "games": matchup_data["games"]
        }
    return None


def get_team_games_in_matchup(team_abbrev: str, matchup_number: int) -> list[int]:
    """
    Get the day indices when a team plays in a given matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        matchup_number: The matchup number.

    Returns:
        List of day indices (0-indexed from matchup start) when the team plays.
    """
    matchup = get_matchup_by_number(matchup_number)
    if not matchup:
        return []

    team_games = matchup["games"].get(team_abbrev, {})
    return sorted([int(day) for day in team_games.keys()])


def get_remaining_games(team_abbrev: str, current_date: Optional[date] = None) -> int:
    """
    Calculate the number of remaining games for a team in the current matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        current_date: The date to calculate from. Defaults to today.

    Returns:
        Number of remaining games in the current matchup.
    """
    if current_date is None:
        current_date = date.today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return 0

    current_day_index = matchup["current_day_index"]
    team_games = matchup["games"].get(team_abbrev, {})

    # Count games on or after the current day
    remaining = sum(1 for day in team_games.keys() if int(day) >= current_day_index)
    return remaining


def get_total_games_in_matchup(team_abbrev: str, matchup_number: int) -> int:
    """
    Get the total number of games for a team in a given matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        matchup_number: The matchup number.

    Returns:
        Total number of games in the matchup for the team.
    """
    matchup = get_matchup_by_number(matchup_number)
    if not matchup:
        return 0

    team_games = matchup["games"].get(team_abbrev, {})
    return len(team_games)


def get_remaining_games_for_matchup(
    team_abbrev: str,
    matchup_number: int,
    current_date: Optional[date] = None
) -> int:
    """
    Calculate remaining games for a team in a specific matchup.

    This is useful when you know the matchup number and want to calculate
    remaining games even if the current date is outside that matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        matchup_number: The matchup number.
        current_date: The date to calculate from. Defaults to today.

    Returns:
        Number of remaining games. Returns total games if matchup hasn't started,
        0 if matchup has ended, otherwise games remaining from current day.
    """
    if current_date is None:
        current_date = date.today()

    matchup = get_matchup_by_number(matchup_number)
    if not matchup:
        return 0

    team_games = matchup["games"].get(team_abbrev, {})
    if not team_games:
        return 0

    start_date = matchup["start_date"]
    end_date = matchup["end_date"]

    # If matchup hasn't started, all games are remaining
    if current_date < start_date:
        return len(team_games)

    # If matchup has ended, no games remaining
    if current_date > end_date:
        return 0

    # Calculate current day index and count remaining games
    current_day_index = (current_date - start_date).days
    remaining = sum(1 for day in team_games.keys() if int(day) >= current_day_index)
    return remaining


def get_matchup_dates(matchup_number: int) -> Optional[tuple[date, date]]:
    """
    Get the start and end dates for a specific matchup.

    Args:
        matchup_number: The matchup number (1-20 for 2025-26 season).

    Returns:
        Tuple of (start_date, end_date) or None if matchup not found.
    """
    matchup = get_matchup_by_number(matchup_number)
    if not matchup:
        return None
    return (matchup["start_date"], matchup["end_date"])


def get_current_matchup_dates(current_date: Optional[date] = None) -> Optional[tuple[date, date]]:
    """
    Get the start and end dates for the current matchup.

    Args:
        current_date: The date to check. Defaults to today.

    Returns:
        Tuple of (start_date, end_date) or None if no current matchup.
    """
    matchup = get_current_matchup(current_date)
    if not matchup:
        return None
    return (matchup["start_date"], matchup["end_date"])
