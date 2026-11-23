"""
Ownership trending serves the newest snapshot that exists — not a computed date.

The pipeline stamps `snapshot_date` on the game-date rule and lands overnight
(~1–4 AM ET), so every wall-clock formula tried here has had a wrong window:
the original Central-calendar read pointed at a not-yet-written row until the
pipeline landed, and a game-date read (`nba_date_et() - 1`) stayed a day
behind from the moment it landed until 6 AM ET — the regression flagged in
review on the cv-core branch. Max-snapshot is right at every hour, so these
tests freeze the exact hours that used to disagree.
"""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from freezegun import freeze_time

from db.models.nba.player_ownership import PlayerOwnership
from db.models.nba.players import Player
from services.ownership_service import OwnershipService

NIGHT = date(2026, 3, 4)  # the game date the newest snapshot is stamped with


def _player(player_id: int) -> None:
    Player.create(
        id=player_id, name=f"P{player_id}", name_normalized=f"p{player_id}",
        espn_id=9000 + player_id,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )


def _snapshot(player_id: int, snapshot_date: date, rost_pct: float) -> None:
    PlayerOwnership.create(
        player=player_id, snapshot_date=snapshot_date, rost_pct=rost_pct,
    )


@pytest.fixture
def trending_rows(integration_db):
    """One riser: 10% eight days ago, 30% on the newest snapshot (last night)."""
    _player(1)
    _snapshot(1, NIGHT - timedelta(days=8), 10.0)
    _snapshot(1, NIGHT - timedelta(days=1), 12.0)
    _snapshot(1, NIGHT, 30.0)
    yield


def _current_of(resp):
    data = resp.data
    players = data.trending_up + data.trending_down
    assert players, resp.message
    return players[0].current_ownership


@pytest.mark.integration
class TestNewestSnapshotWinsAtEveryHour:
    @pytest.mark.parametrize("frozen", [
        "2026-03-05T08:00:00Z",  # 3:00 AM ET — pipeline just landed; game date still Mar 4
        "2026-03-05T10:30:00Z",  # 5:30 AM ET — the hour the game-date read was a day behind
        "2026-03-05T12:00:00Z",  # 7:00 AM ET — every formula agreed here
        "2026-03-05T23:00:00Z",  # 6:00 PM ET
    ])
    def test_serves_last_nights_snapshot(self, trending_rows, frozen):
        with freeze_time(frozen):
            resp = asyncio.run(OwnershipService.get_trending(days=7))
        assert _current_of(resp) == 30.0

    def test_lookback_is_measured_from_the_served_snapshot(self, trending_rows):
        """past = closest snapshot ≤ (current_date - days), regardless of clock."""
        with freeze_time("2026-03-05T10:30:00Z"):
            resp = asyncio.run(OwnershipService.get_trending(days=7))
        player = (resp.data.trending_up + resp.data.trending_down)[0]
        assert player.previous_ownership == 10.0  # the day-8 row, not the day-1 row
        assert player.change == 20.0

    def test_no_snapshots_is_a_clean_empty_response(self, integration_db):
        resp = asyncio.run(OwnershipService.get_trending(days=7))
        assert resp.data.trending_up == [] and resp.data.trending_down == []


def _trending(days: int = 7):
    with freeze_time("2026-03-05T12:00:00Z"):
        resp = asyncio.run(OwnershipService.get_trending(days=days, min_change=0, min_ownership=0))
    return {p.player_id: p for p in resp.data.trending_up + resp.data.trending_down}


@pytest.mark.integration
class TestAPlayerMissingFromTheBaselineIsNotZero:
    """A partial baseline snapshot must not read a missing player as 0% owned.

    The 2026-11-14 run matched 594 of ~620 players, and the 7-day Adds list
    then showed Donovan Mitchell going 0% -> 99.9%.
    """

    @pytest.fixture
    def partial_baseline(self, integration_db):
        baseline = NIGHT - timedelta(days=7)
        # Player 1 is in every snapshot: the baseline exists and is used.
        _player(1)
        _snapshot(1, baseline, 10.0)
        _snapshot(1, NIGHT, 30.0)
        # Player 2 is a star the baseline snapshot skipped; he was read the day before.
        _player(2)
        _snapshot(2, baseline - timedelta(days=1), 99.8)
        _snapshot(2, NIGHT, 99.9)
        # Player 3 has no reading at or before the baseline at all.
        _player(3)
        _snapshot(3, NIGHT - timedelta(days=3), 40.0)
        _snapshot(3, NIGHT, 45.0)
        # Player 4's only earlier reading is older than the fill window.
        _player(4)
        _snapshot(4, baseline - timedelta(days=30), 5.0)
        _snapshot(4, NIGHT, 60.0)
        yield

    def test_the_baseline_snapshot_still_wins(self, partial_baseline):
        p = _trending()[1]
        assert (p.previous_ownership, p.change) == (10.0, 20.0)

    def test_a_skipped_player_uses_his_reading_from_the_day_before(self, partial_baseline):
        p = _trending()[2]
        assert p.previous_ownership == 99.8
        assert p.change == 0.1

    def test_no_reading_in_the_window_is_left_off_not_read_as_zero(self, partial_baseline):
        players = _trending()
        assert 3 not in players
        assert 4 not in players

    def test_no_baseline_snapshot_at_all_lists_nobody(self, integration_db):
        _player(1)
        _snapshot(1, NIGHT - timedelta(days=2), 10.0)
        _snapshot(1, NIGHT, 30.0)
        assert _trending(days=7) == {}
