import json
import os
from datetime import datetime, date
import pytz
from typing import Optional
from pathlib import Path


# Load schedule data at module level
_SCHEDULE_DATA: dict = {}
_SCHEDULE_DATA_V2: dict = {}

def _load_schedule() -> dict:
    """Load the schedule JSON file."""
    global _SCHEDULE_DATA
    if not _SCHEDULE_DATA:
        schedule_path = Path(__file__).parent.parent / "static" / "schedule25-26.json"
        with open(schedule_path, "r") as f:
            _SCHEDULE_DATA = json.load(f)
    return _SCHEDULE_DATA


def _load_schedule_v2() -> dict:
    """Load the schedule JSON file."""
    global _SCHEDULE_DATA_V2
    if not _SCHEDULE_DATA_V2:
        schedule_path = Path(__file__).parent.parent / "static" / "matchupsPerDay25-26.json"
        with open(schedule_path, "r") as f:
            _SCHEDULE_DATA_V2 = json.load(f)
    return _SCHEDULE_DATA_V2

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
    central_tz = pytz.timezone("US/Central")
    if current_date is None:
        current_date = datetime.now(central_tz).date()

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
    central_tz = pytz.timezone("US/Central")
    if current_date is None:
        current_date = datetime.now(central_tz).date()

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


def get_remaining_game_days(team_abbrev: str, current_date: Optional[date] = None) -> list[int]:
    """
    Get the list of remaining game day indices for a team in the current matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        current_date: The date to calculate from. Defaults to today.

    Returns:
        List of day indices (0-indexed) for remaining games.
    """
    if current_date is None:
        current_date = date.today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return []

    current_day_index = matchup["current_day_index"]
    team_games = matchup["games"].get(team_abbrev, {})

    return sorted([int(day) for day in team_games.keys() if int(day) >= current_day_index])


def _find_b2b_pairs(game_days: list[int]) -> list[tuple[int, int]]:
    """
    Find consecutive day pairs (back-to-backs) in a list of game days.

    Args:
        game_days: Sorted list of day indices when team plays.

    Returns:
        List of (day1, day2) tuples representing back-to-back games.
    """
    b2b_pairs = []
    for i in range(len(game_days) - 1):
        if game_days[i + 1] - game_days[i] == 1:
            b2b_pairs.append((game_days[i], game_days[i + 1]))
    return b2b_pairs


def has_remaining_b2b(team_abbrev: str, current_date: Optional[date] = None) -> bool:
    """
    Check if a team has any remaining back-to-back games in the current matchup.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        current_date: The date to check from. Defaults to today.

    Returns:
        True if the team has at least one remaining B2B sequence, False otherwise.
    """
    remaining_days = get_remaining_game_days(team_abbrev, current_date)
    b2b_pairs = _find_b2b_pairs(remaining_days)
    return len(b2b_pairs) > 0


def get_b2b_game_count(team_abbrev: str, current_date: Optional[date] = None) -> int:
    """
    Count how many remaining games are part of back-to-back sequences.

    A B2B means 2 consecutive days with games. This returns the count of individual
    game days that are part of remaining B2B sequences.

    Args:
        team_abbrev: Team abbreviation (e.g., 'LAL', 'GSW').
        current_date: The date to calculate from. Defaults to today.

    Returns:
        Number of game days that are part of remaining B2B sequences.
        Example: If team has B2B on days 3-4 and 6-7, returns 4.
    """
    remaining_days = get_remaining_game_days(team_abbrev, current_date)
    b2b_pairs = _find_b2b_pairs(remaining_days)

    # Collect unique days that are part of B2Bs
    b2b_days = set()
    for day1, day2 in b2b_pairs:
        b2b_days.add(day1)
        b2b_days.add(day2)

    return len(b2b_days)


def get_teams_with_b2b(current_date: Optional[date] = None) -> list[str]:
    """
    Get list of team abbreviations with remaining B2B games in the current matchup.

    Args:
        current_date: The date to check from. Defaults to today.

    Returns:
        List of team abbreviations that have at least one remaining B2B.
    """
    if current_date is None:
        current_date = date.today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return []

    teams_with_b2b = []
    for team_abbrev in matchup["games"].keys():
        if has_remaining_b2b(team_abbrev, current_date):
            teams_with_b2b.append(team_abbrev)

    return sorted(teams_with_b2b)


def get_upcoming_games_on_date(date: date) -> list[dict]:
    """
    Get the upcoming games on a specific date.

    Args:
        date: The date to check.

    Returns:
        List of upcoming games on the date.
    """
    schedule = _load_schedule_v2()
    games = schedule.get(date.strftime("%m/%d/%Y"), [])
    return games