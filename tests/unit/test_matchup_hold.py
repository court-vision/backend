"""
services.matchup_hold: the provider read behind the live matchup, kept for a
few seconds so a Week desk on the 15 s beat does not ask ESPN on every poll,
and the rules that keep an old lineup from being handed back.
"""

import pytest

from core.inflight import provider_write
from schemas.common import ApiStatus
from schemas.matchup import MatchupResp
from services import matchup_hold

pytestmark = pytest.mark.unit

USER, TEAM = 7, 28
READ = MatchupResp(status=ApiStatus.SUCCESS, message="read", data=None)


@pytest.fixture
def clock(monkeypatch):
    """The module's two clocks, moved together: `clock.tick(5)` is five seconds on."""

    class Clock:
        now = 1_000.0

        def tick(self, seconds: float) -> None:
            self.now += seconds

    c = Clock()
    monkeypatch.setattr(matchup_hold.time, "monotonic", lambda: c.now)
    monkeypatch.setattr(matchup_hold.time, "time", lambda: 1_763_256_000.0 + c.now)
    return c


def keep(user_id: int = USER, team_id: int = TEAM) -> matchup_hold.Kept:
    return matchup_hold.keep(user_id, team_id, READ, matchup_hold.reading())


def test_a_caller_holding_the_read_gets_it_again(clock) -> None:
    kept = keep()
    clock.tick(15)
    again = matchup_hold.reusable(USER, TEAM, kept.read_ms)
    assert again is kept and again.matchup is READ


def test_a_caller_with_no_stamp_never_reuses(clock) -> None:
    """A first read, and the read a desk makes after a move."""
    keep()
    assert matchup_hold.reusable(USER, TEAM, None) is None


def test_nothing_kept_is_nothing_to_reuse(clock) -> None:
    assert matchup_hold.reusable(USER, TEAM, 1) is None


def test_a_read_is_kept_for_hold_seconds(clock) -> None:
    kept = keep()
    clock.tick(matchup_hold.HOLD_SECONDS)
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms) is kept
    clock.tick(0.1)
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms) is None


def test_the_15_second_beat_reads_the_provider_on_every_other_poll(clock) -> None:
    """Polls at 0, 15, 30, 45 s: the provider is read at 0 and 30."""
    provider_reads = 0
    stamp = None
    for _ in range(4):
        kept = matchup_hold.reusable(USER, TEAM, stamp)
        if kept is None:
            provider_reads += 1
            kept = keep()
        stamp = kept.read_ms
        clock.tick(15)
    assert provider_reads == 2


def test_a_read_from_before_a_write_is_not_handed_back(clock) -> None:
    kept = keep()
    with provider_write():
        pass
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms) is None


def test_a_read_that_began_during_a_write_is_not_handed_back(clock) -> None:
    """It may have reached ESPN before the write did."""
    with provider_write():
        epoch = matchup_hold.reading()
    kept = matchup_hold.keep(USER, TEAM, READ, epoch)
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms) is None


def test_a_read_older_than_the_callers_is_not_handed_back(clock) -> None:
    """Another replica served the caller a newer one."""
    kept = keep()
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms + 1) is None
    # The same or an older stamp is fine: two tabs of one team share the newest read.
    kept = keep()
    assert matchup_hold.reusable(USER, TEAM, kept.read_ms - 5_000) is kept


def test_a_read_is_its_teams_and_its_owners(clock) -> None:
    kept = keep()
    assert matchup_hold.reusable(USER, TEAM + 1, kept.read_ms) is None
    assert matchup_hold.reusable(USER + 1, TEAM, kept.read_ms) is None


def test_only_the_newest_teams_are_kept(clock, monkeypatch) -> None:
    monkeypatch.setattr(matchup_hold, "MAX_TEAMS", 2)
    first, second, third = keep(team_id=1), keep(team_id=2), keep(team_id=3)
    assert matchup_hold.reusable(USER, 1, first.read_ms) is None
    assert matchup_hold.reusable(USER, 2, second.read_ms) is second
    assert matchup_hold.reusable(USER, 3, third.read_ms) is third
