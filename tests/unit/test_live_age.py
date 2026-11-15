"""
core.live_age: how old the live pipeline's newest write is, which the live
answers carry as `live_age_ms` so a screen can time its next read to the next
write.
"""

from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from core.live_age import live_age_ms

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 16, 1, 30, 16)      # UTC and naive, as the pipeline stamps its writes


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def test_the_age_is_the_newest_writes() -> None:
    """A night's lines are written a game at a time: the last game written is the newest word."""
    assert live_age_ms([ago(15.9), ago(1.2), ago(4.0)], NOW) == 1200


def test_nothing_written_has_no_age() -> None:
    assert live_age_ms([], NOW) is None
    assert live_age_ms([None, None], NOW) is None
    assert live_age_ms([None, ago(2)], NOW) == 2000


def test_a_write_stamped_ahead_of_this_clock_is_new_not_negative() -> None:
    """data-platform stamps the write with its own clock, which can run ahead of the API's."""
    assert live_age_ms([ago(-0.4)], NOW) == 0


def test_the_clock_read_is_utc_and_naive_like_the_stamps() -> None:
    with freeze_time("2026-11-16T01:30:16Z"):
        assert live_age_ms([ago(3)]) == 3000
