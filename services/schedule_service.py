"""
Fantasy-week calendar for the active season.

Two generated static files per season (see scripts/build_season_calendar.py):

- static/schedule{yy}-{yy}.json — the fantasy weeks: {"schedule": {"1": {startDate,
  endDate, gameSpan, games: {TRICODE: {"dayIdx": true}}}}}. Week ids match ESPN's
  weekly scoring-period ids (the All-Star weeks are merged into one 14-day week).
- static/matchupsPerDay{yy}-{yy}.json — {"MM/DD/YYYY": [{homeTeam, awayTeam}]} for
  every game date, preseason included.

The season is `settings.nba_season` (derived from today's date unless pinned).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal, Optional

import pytz

from core.logging import get_logger
from core.season import espn_year_for, short_key
from core.settings import settings

STATIC_DIR = Path(__file__).parent.parent / "static"
CalendarKind = Literal["schedule", "matchupsPerDay"]
SeasonPhase = Literal["preseason", "regular", "offseason"]

_CACHE: dict[tuple[str, str], dict] = {}
log = get_logger()


# ---- files -----------------------------------------------------------------


def _season(season: Optional[str]) -> str:
    return season or settings.nba_season


def calendar_path(kind: CalendarKind, season: Optional[str] = None) -> Path:
    return STATIC_DIR / f"{kind}{short_key(_season(season))}.json"


def _load(kind: CalendarKind, season: Optional[str] = None) -> dict:
    season = _season(season)
    key = (kind, season)
    if key not in _CACHE:
        path = calendar_path(kind, season)
        if not path.exists():
            raise FileNotFoundError(
                f"No {kind} calendar for season {season}: expected {path.name} in {STATIC_DIR} "
                "(run scripts/build_season_calendar.py)"
            )
        with open(path, "r") as f:
            _CACHE[key] = json.load(f)
    return _CACHE[key]


def _load_schedule(season: Optional[str] = None) -> dict:
    """The fantasy-week file (top-level {"schedule": {...}})."""
    return _load("schedule", season)


def _load_schedule_v2(season: Optional[str] = None) -> dict:
    """The per-day matchups file."""
    return _load("matchupsPerDay", season)


def reset_cache() -> None:
    """Forget loaded calendars (tests switch `settings.nba_season` between cases)."""
    _CACHE.clear()


def assert_calendar_available(season: Optional[str] = None) -> None:
    """Fail fast at startup if the season's calendar files are missing."""
    _load_schedule(season)
    _load_schedule_v2(season)
    log.info("calendar_loaded", season=_season(season), weeks=get_max_week(season))


def _parse_date(date_str: str) -> date:
    """Parse date string in MM/DD/YYYY format."""
    return datetime.strptime(date_str, "%m/%d/%Y").date()


# ---- today -----------------------------------------------------------------


def get_nba_today() -> date:
    """The ET fantasy day: before 2 AM ET counts as yesterday."""
    return _get_nba_today()


def _get_nba_today() -> date:
    """Return the current fantasy scheduling date in ET.

    Before 2 AM ET counts as yesterday — aligns with when ESPN's batch update
    runs (~2 AM ET), after which the new fantasy day becomes active.

    DO NOT collapse this onto core.nba_calendar.nba_date_et. That is the NBA
    *game*-date rule (6 AM ET); this is the ESPN *fantasy*-day rule (2 AM ET),
    and they deliberately disagree for four hours a night. Conflating them is
    not hypothetical: it dropped a full day off live matchup scores whenever
    ESPN's batch ran late (see matchup_service's cutover notes).
    """
    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)
    if now_et.hour < 2:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


# ---- weeks -----------------------------------------------------------------


def _week_dict(matchup_num: int, matchup_data: dict) -> dict:
    return {
        "matchup_number": int(matchup_num),
        "start_date": _parse_date(matchup_data["startDate"]),
        "end_date": _parse_date(matchup_data["endDate"]),
        "game_span": matchup_data["gameSpan"],
        "games": matchup_data["games"],
    }


def iter_weeks(season: Optional[str] = None) -> Iterator[dict]:
    """Every fantasy week in order: {matchup_number, start_date, end_date, game_span, games}."""
    schedule = _load_schedule(season).get("schedule", {})
    for num, data in sorted(schedule.items(), key=lambda kv: int(kv[0])):
        yield _week_dict(int(num), data)


def get_max_week(season: Optional[str] = None) -> int:
    schedule = _load_schedule(season).get("schedule", {})
    return max((int(k) for k in schedule), default=0)


def get_current_matchup(current_date: Optional[date] = None) -> Optional[dict]:
    """
    Get the current matchup info based on the provided date.

    Args:
        current_date: The date to check. Defaults to the ET fantasy day
                      (before 2 AM ET counts as yesterday).

    Returns:
        Dict with 'matchup_number', 'start_date', 'end_date', 'game_span',
        'games', and 'current_day_index', or None outside the calendar.
    """
    if current_date is None:
        current_date = _get_nba_today()

    for week in iter_weeks():
        if week["start_date"] <= current_date <= week["end_date"]:
            week["current_day_index"] = (current_date - week["start_date"]).days
            return week
    return None


def get_matchup_by_number(matchup_number: int) -> Optional[dict]:
    """Matchup info for a week number (1-based), or None if it isn't in the calendar."""
    schedule = _load_schedule().get("schedule", {})
    matchup_data = schedule.get(str(matchup_number))
    if matchup_data:
        return _week_dict(int(matchup_number), matchup_data)
    return None


def get_remaining_games(team_abbrev: str, current_date: Optional[date] = None) -> int:
    """Number of games on or after `current_date` for a team in the current matchup."""
    if current_date is None:
        current_date = _get_nba_today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return 0

    current_day_index = matchup["current_day_index"]
    team_games = matchup["games"].get(team_abbrev, {})
    return sum(1 for day in team_games.keys() if int(day) >= current_day_index)


def get_matchup_dates(matchup_number: int) -> Optional[tuple[date, date]]:
    """(start_date, end_date) for a week number, or None."""
    matchup = get_matchup_by_number(matchup_number)
    if not matchup:
        return None
    return (matchup["start_date"], matchup["end_date"])


def get_dates_for_scoring_periods(scoring_periods: list[int]) -> Optional[tuple[date, date]]:
    """
    Combined date range covering the given ESPN *weekly* scoring-period ids.

    Playoff rounds map one matchup period to several ids (e.g. [22, 23]).
    Ids outside the calendar are ignored; None when none match.
    """
    schedule = _load_schedule().get("schedule", {})
    starts, ends = [], []
    for sp in scoring_periods:
        entry = schedule.get(str(sp))
        if entry:
            starts.append(_parse_date(entry["startDate"]))
            ends.append(_parse_date(entry["endDate"]))
    if not starts:
        return None
    return (min(starts), max(ends))


# ---- season bounds ---------------------------------------------------------


@dataclass(frozen=True)
class SeasonBounds:
    season: str
    espn_year: int
    opening_night: date
    regular_season_end: date
    preseason_start: Optional[date]
    week_count: int


def get_season_bounds(season: Optional[str] = None) -> SeasonBounds:
    season = _season(season)
    weeks = list(iter_weeks(season))
    if not weeks:
        raise ValueError(f"Calendar for {season} has no weeks")
    per_day = _load_schedule_v2(season)
    first_day = min((_parse_date(k) for k in per_day), default=None)
    opening = weeks[0]["start_date"]
    preseason_start = first_day if first_day is not None and first_day < opening else None
    return SeasonBounds(
        season=season,
        espn_year=espn_year_for(season),
        opening_night=opening,
        regular_season_end=weeks[-1]["end_date"],
        preseason_start=preseason_start,
        week_count=len(weeks),
    )


def get_season_phase(today: Optional[date] = None, season: Optional[str] = None) -> SeasonPhase:
    """preseason (from the first preseason game to opening night), regular, or offseason."""
    today = today or _get_nba_today()
    b = get_season_bounds(season)
    if today < b.opening_night:
        return "preseason" if b.preseason_start is not None and today >= b.preseason_start else "offseason"
    if today <= b.regular_season_end:
        return "regular"
    return "offseason"


def season_day(today: Optional[date] = None, season: Optional[str] = None) -> Optional[int]:
    """1-based day of the regular season (ESPN's scoringPeriodId), None outside it."""
    today = today or _get_nba_today()
    b = get_season_bounds(season)
    if b.opening_night <= today <= b.regular_season_end:
        return (today - b.opening_night).days + 1
    return None


def date_for_espn_scoring_period(period_id: int, season: Optional[str] = None) -> date:
    """ESPN's day-granular scoringPeriodId → calendar date (1 = opening night)."""
    return get_season_bounds(season).opening_night + timedelta(days=int(period_id) - 1)


# ---- provider period resolution --------------------------------------------


def espn_scoring_periods(matchup_period_map: Optional[dict], current_matchup_period: int) -> list[int]:
    """ESPN weekly scoring-period ids for a matchup period (falls back to the id itself)."""
    ids = matchup_period_map.get(str(current_matchup_period)) if matchup_period_map else None
    if not ids:
        return [int(current_matchup_period)]
    return [int(x) for x in ids]


def get_espn_matchup_dates(
    matchup_period_map: Optional[dict],
    current_matchup_period: int,
    latest_scoring_period: Optional[int] = None,
) -> Optional[tuple[date, date]]:
    """
    Date range of an ESPN matchup period.

    Primary: the league's `scheduleSettings.matchupPeriods` weekly ids indexed
    into our calendar (they match when ESPN merges the All-Star weeks the way
    the calendar does). Safety net: `status.latestScoringPeriod` is a DAY index,
    so if that day falls outside the resolved range (or nothing resolved), the
    calendar week containing that day wins and a warning is logged — the
    current week stays right even if ESPN's numbering diverges from ours.
    """
    ids = espn_scoring_periods(matchup_period_map, current_matchup_period)
    dates = get_dates_for_scoring_periods(ids)

    if latest_scoring_period:
        try:
            today = date_for_espn_scoring_period(int(latest_scoring_period))
        except (TypeError, ValueError, FileNotFoundError):
            today = None
        if today is not None and (dates is None or not (dates[0] <= today <= dates[1])):
            week = get_current_matchup(today)
            if week is not None:
                log.warning(
                    "espn_period_misaligned",
                    matchup_period=current_matchup_period, scoring_periods=ids,
                    latest_scoring_period=latest_scoring_period, day=str(today),
                    resolved=[str(d) for d in dates] if dates else None,
                    calendar_week=week["matchup_number"],
                )
                return (week["start_date"], week["end_date"])
    return dates


# ---- team schedule helpers -------------------------------------------------


def get_remaining_game_days(team_abbrev: str, current_date: Optional[date] = None) -> list[int]:
    """Remaining game day indices (0-indexed) for a team in the current matchup."""
    if current_date is None:
        current_date = _get_nba_today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return []

    current_day_index = matchup["current_day_index"]
    team_games = matchup["games"].get(team_abbrev, {})
    return sorted([int(day) for day in team_games.keys() if int(day) >= current_day_index])


def _find_b2b_pairs(game_days: list[int]) -> list[tuple[int, int]]:
    """Consecutive day pairs (back-to-backs) in a sorted list of day indices."""
    return [(game_days[i], game_days[i + 1]) for i in range(len(game_days) - 1)
            if game_days[i + 1] - game_days[i] == 1]


def has_remaining_b2b(team_abbrev: str, current_date: Optional[date] = None) -> bool:
    """True if the team has at least one remaining back-to-back in the current matchup."""
    return len(_find_b2b_pairs(get_remaining_game_days(team_abbrev, current_date))) > 0


def get_b2b_game_count(team_abbrev: str, current_date: Optional[date] = None) -> int:
    """Number of remaining game days that are part of a back-to-back (days 3-4 and 6-7 → 4)."""
    b2b_days: set[int] = set()
    for day1, day2 in _find_b2b_pairs(get_remaining_game_days(team_abbrev, current_date)):
        b2b_days.add(day1)
        b2b_days.add(day2)
    return len(b2b_days)


def get_teams_with_b2b(current_date: Optional[date] = None) -> list[str]:
    """Teams with at least one remaining back-to-back in the current matchup."""
    if current_date is None:
        current_date = _get_nba_today()

    matchup = get_current_matchup(current_date)
    if not matchup:
        return []
    return sorted(team for team in matchup["games"].keys() if has_remaining_b2b(team, current_date))


def get_upcoming_games_on_date(date: date) -> list[dict]:
    """Games on a date from the per-day file ([{homeTeam, awayTeam}]; [] if none)."""
    return _load_schedule_v2().get(date.strftime("%m/%d/%Y"), [])
