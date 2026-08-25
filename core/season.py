"""
Season keys — the one place that knows how "2026-27" is derived and formatted.

The NBA season string flips on August 1 (month >= 8 → the season that starts
this calendar year). `settings.nba_season` / `settings.espn_year` default to
these derivations; the env vars remain overrides.
"""

from __future__ import annotations

import re
from datetime import date

_SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")


def season_for_date(d: date) -> str:
    """"2026-27" for any date from 2026-08-01 through 2027-07-31."""
    start_year = d.year if d.month >= 8 else d.year - 1
    return season_from_year(start_year)


def season_key(today: date | None = None) -> str:
    """The current NBA season key, e.g. "2026-27"."""
    return season_for_date(today or date.today())


def season_from_year(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def validate_season(season: str) -> str:
    """Return the season unchanged if it looks like YYYY-YY with consecutive years."""
    m = _SEASON_RE.match(season or "")
    if not m or int(m.group(2)) != (int(m.group(1)) + 1) % 100:
        raise ValueError(f"Season must look like '2026-27', got {season!r}")
    return season


def start_year(season: str) -> int:
    return int(validate_season(season)[:4])


def espn_year_for(season: str) -> int:
    """ESPN's seasonId is the calendar year the season ends in: 2026-27 → 2027."""
    return start_year(season) + 1


def previous_season(season: str) -> str:
    return season_from_year(start_year(season) - 1)


def next_season(season: str) -> str:
    return season_from_year(start_year(season) + 1)


def short_key(season: str) -> str:
    """File suffix used by the static calendars: "2026-27" → "26-27"."""
    s = validate_season(season)
    return f"{s[2:4]}-{s[5:7]}"


def season_label(season: str) -> str:
    """Display form with an en dash: "2026–27"."""
    s = validate_season(season)
    return f"{s[:4]}–{s[5:7]}"
