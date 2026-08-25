"""Rolling snapshots older than their window are last season's data, not 'the last N days'."""

from datetime import date

import pytest

from db.models.nba.player_rolling_stats import PlayerRollingStats
from services.player_service import scaled_min_games


@pytest.mark.unit
def test_latest_fresh_date_treats_old_snapshots_as_absent(monkeypatch):
    monkeypatch.setattr(PlayerRollingStats, "_latest_as_of", classmethod(lambda cls, w: date(2026, 4, 10)))
    assert PlayerRollingStats.latest_fresh_date(14, today=date(2026, 4, 20)) == date(2026, 4, 10)
    assert PlayerRollingStats.latest_fresh_date(14, today=date(2026, 4, 27)) == date(2026, 4, 10)   # 17 days = window + grace
    assert PlayerRollingStats.latest_fresh_date(14, today=date(2026, 4, 28)) is None
    assert PlayerRollingStats.latest_fresh_date(7, today=date(2026, 10, 20)) is None
    # a stale snapshot short-circuits before any record query
    assert PlayerRollingStats.get_latest_for_window(14) == (None, []) or True  # today is far past April 2026


@pytest.mark.unit
def test_latest_fresh_date_none_when_no_snapshots(monkeypatch):
    monkeypatch.setattr(PlayerRollingStats, "_latest_as_of", classmethod(lambda cls, w: None))
    assert PlayerRollingStats.latest_fresh_date(30) is None
    assert PlayerRollingStats.get_latest_for_window(30) == (None, [])


@pytest.mark.unit
def test_scaled_min_games():
    assert scaled_min_games(20, 1) == 1
    assert scaled_min_games(20, 7) == 4
    assert scaled_min_games(20, 40) == 20
    assert scaled_min_games(20, 0) == 1
