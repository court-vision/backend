"""
Unit tests for the NBA date convention.

NBA rule: before 6 AM ET = yesterday's date (games from the previous night
are still "today's" games until 6 AM ET the next morning).

Tests the _get_nba_today() function from services.games_service.
"""

import pytest
from datetime import date
from freezegun import freeze_time

from services.games_service import _get_nba_today


@pytest.mark.unit
class TestNBADateConvention:
    """NBA date: before 6 AM ET = yesterday."""

    @pytest.mark.parametrize("utc_time,expected", [
        # Before 6 AM ET (ET = UTC-5 in March) → yesterday
        ("2026-03-05T00:00:00Z", date(2026, 3, 4)),  # midnight ET
        ("2026-03-05T04:59:59Z", date(2026, 3, 4)),  # 11:59 PM ET prev day
        ("2026-03-05T06:00:00Z", date(2026, 3, 4)),  # 1:00 AM ET
        ("2026-03-05T10:59:59Z", date(2026, 3, 4)),  # 5:59 AM ET
        # At and after 6 AM ET → today
        ("2026-03-05T11:00:00Z", date(2026, 3, 5)),  # 6:00 AM ET exactly
        ("2026-03-05T15:00:00Z", date(2026, 3, 5)),  # 10:00 AM ET
        ("2026-03-05T20:00:00Z", date(2026, 3, 5)),  # 3:00 PM ET
        ("2026-03-06T02:00:00Z", date(2026, 3, 5)),  # 9:00 PM ET
    ])
    def test_nba_today_boundary(self, utc_time, expected):
        with freeze_time(utc_time):
            assert _get_nba_today() == expected

    def test_post_midnight_returns_yesterday(self, freeze_post_midnight):
        """3 AM ET (8 AM UTC) — the fixture sets us in the early-morning window."""
        assert _get_nba_today() == freeze_post_midnight

    def test_evening_returns_today(self, freeze_game_night):
        """9 PM ET (2 AM UTC next day) — games are live, NBA date = today."""
        assert _get_nba_today() == freeze_game_night

    def test_morning_returns_today(self, freeze_morning):
        """10 AM ET (3 PM UTC) — well past 6 AM ET, NBA date = today."""
        assert _get_nba_today() == freeze_morning
