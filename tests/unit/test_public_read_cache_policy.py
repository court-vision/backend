"""The public read caches: their kill switch, keys and memory budget, and the
fantasy-day rollover that bounds how long a client keeps schedule weeks."""

from datetime import date, datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from core import public_read_cache as caches
from core.settings import settings
from services import schedule_service

pytestmark = pytest.mark.unit


def test_every_cache_is_bounded_by_entries_and_bytes():
    for cache in caches.ALL_CACHES:
        assert cache.max_entries > 0 and cache.max_bytes is not None and cache.max_bytes > 0, cache.name

    total = sum(cache.max_bytes for cache in caches.ALL_CACHES)
    assert total == pytest.approx(22.3 * caches.MB, rel=0.01)   # what the module docstring promises


def test_every_cache_is_listed_once_for_clearing():
    named = [v for k, v in vars(caches).items() if isinstance(v, caches.ResponseCache)]

    assert len(caches.ALL_CACHES) == len(set(map(id, caches.ALL_CACHES))) == len(named)
    assert {c.name for c in caches.ALL_CACHES} == {c.name for c in named}


def test_ttls_follow_the_data_cadence():
    daily = [caches.PLAYER_STATS, caches.PLAYER_PROFILE, caches.PLAYER_PROJECTION, caches.PLAYER_PERCENTILES,
             caches.PLAYER_TRENDS, caches.TEAM_SCHEDULE, caches.TEAM_STATS, caches.TEAM_ROSTER, caches.GAMES]
    assert {c.ttl_seconds for c in daily} == {300.0}
    assert caches.GAMES_SETTLED.ttl_seconds == caches.SCHEDULE_WEEKS.ttl_seconds == 3600.0


def test_the_kill_switch_builds_disabled_caches(monkeypatch):
    monkeypatch.setattr(settings, "public_read_cache_enabled", False)

    assert caches._cache("x", 300.0, 10, 100).enabled is False


def test_a_key_carries_the_active_season(monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2025-26")
    first = caches.key(1, None, "l5")
    monkeypatch.setattr(settings, "nba_season", "2026-27")

    assert first == (1, None, "l5", "2025-26")
    assert caches.key(1, None, "l5") != first


def test_clear_all_empties_every_cache():
    import asyncio

    async def fill():
        for cache in caches.ALL_CACHES:
            await cache.get_or_render("k", _body)

    async def _body():
        return b"{}"

    asyncio.run(fill())
    caches.clear_all()

    assert all(c.stats()["entries"] == 0 for c in caches.ALL_CACHES)


# ---- seconds_left_in_fantasy_day ----

def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,day,expected", [
    (utc(2026, 3, 4, 6, 50), date(2026, 3, 3), 600),          # 1:50 AM EST: ten minutes left
    (utc(2026, 3, 4, 7, 0), date(2026, 3, 4), 86_400),        # 2:00 AM EST: a fresh day
    (utc(2026, 3, 4, 20, 0), date(2026, 3, 4), 11 * 3600),    # 3 PM EST
    (utc(2026, 10, 14, 5, 30), date(2026, 10, 13), 1800),     # 1:30 AM EDT: still the 13th
])
def test_seconds_left_counts_to_the_next_2am_eastern(now, day, expected):
    assert schedule_service.seconds_left_in_fantasy_day(day, now) == expected


def test_seconds_left_across_the_spring_forward_night():
    """2026-03-08: 2 AM EST never happens; the clock jumps to 3 AM EDT (07:00 UTC)."""
    assert schedule_service.seconds_left_in_fantasy_day(date(2026, 3, 7), utc(2026, 3, 8, 6, 59)) == 60


def test_seconds_left_across_the_fall_back_night():
    """2026-11-01: 1 AM-2 AM repeats; the day turns at 2 AM EST (07:00 UTC), after both."""
    assert schedule_service.seconds_left_in_fantasy_day(date(2026, 10, 31), utc(2026, 11, 1, 5, 30)) == 5400


def test_a_day_already_over_has_one_second_left():
    """A body built a moment before the rollover and served after it is not kept."""
    assert schedule_service.seconds_left_in_fantasy_day(date(2026, 3, 3), utc(2026, 3, 4, 7, 0, 5)) == 1


@pytest.mark.parametrize("start", [
    "2026-03-04T15:00:00Z", "2026-03-08T06:30:00Z", "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z",
    "2026-12-31T06:59:00Z",
])
def test_the_rollover_is_exactly_when_get_nba_today_moves_on(start):
    with freeze_time(start):
        today = schedule_service.get_nba_today()
        left = schedule_service.seconds_left_in_fantasy_day(today)
    moment = datetime.fromisoformat(start.replace("Z", "+00:00"))

    with freeze_time(moment + timedelta(seconds=left - 1)):
        assert schedule_service.get_nba_today() == today
    with freeze_time(moment + timedelta(seconds=left)):
        assert schedule_service.get_nba_today() == today + timedelta(days=1)
