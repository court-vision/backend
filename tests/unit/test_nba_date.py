"""
Unit tests for the NBA date convention.

NBA rule: before 6 AM ET = yesterday's date (games from the previous night
are still "today's" games until 6 AM ET the next morning).

One definition now — cv_core.nba_calendar.nba_date_et — where this repo used
to carry four private copies (games_service, live routes, the live-game
service, matchup_days). The delegation tests pin each historical name to the
shared rule so a fifth copy cannot quietly reappear.
"""

import pytest
from datetime import date, datetime
from freezegun import freeze_time

from core.nba_calendar import EASTERN, nba_date_et
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


@pytest.mark.unit
class TestOneDefinition:
    """Every historical name resolves to cv_core's rule."""

    def test_games_service_is_the_shared_function(self):
        assert _get_nba_today is nba_date_et

    def test_live_game_service_is_the_shared_function(self):
        from services import nba_team_live_game_service as svc
        assert svc._get_nba_today is nba_date_et

    def test_live_routes_are_the_shared_function(self):
        from api.v1.public import live
        assert live._get_nba_date is nba_date_et

    def test_matchup_days_delegates(self):
        from services.matchup_days import nba_today
        with freeze_time("2026-03-05T10:59:00Z"):
            assert nba_today() == nba_date_et() == date(2026, 3, 4)

    def test_the_fantasy_day_rule_is_deliberately_different(self):
        """schedule_service's 2 AM ET rule must NOT collapse onto this one."""
        from services.schedule_service import _get_nba_today as fantasy_day
        assert fantasy_day is not nba_date_et
        with freeze_time("2026-03-05T09:00:00Z"):  # 4:00 AM ET
            assert fantasy_day() == date(2026, 3, 5)   # past 2 AM: today
            assert nba_date_et() == date(2026, 3, 4)   # before 6 AM: yesterday


@pytest.mark.unit
class TestExplicitNow:
    """nba_date_et(now=...) accepts aware datetimes in any zone, and naive as ET."""

    def test_aware_other_zone(self):
        import pytz
        moment = EASTERN.localize(datetime(2026, 3, 5, 5, 59)).astimezone(pytz.utc)
        assert nba_date_et(moment) == date(2026, 3, 4)

    def test_naive_reads_as_eastern(self):
        assert nba_date_et(datetime(2026, 3, 5, 6, 0)) == date(2026, 3, 5)
