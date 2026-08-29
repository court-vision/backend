"""
Integration: nba.rankings.rank_change measures a fixed 7 days.

Snapshots land only on days a player's GP changed, so the pre-0007 `LEAD(fpts, 5)`
reached back a different amount of wall-clock time for every player. These run
against the real migration chain, which is the only place that SQL exists.
"""

from datetime import date, datetime

import pytest

from db.base import db
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.stats.rankings import Rankings

SEASON = "2026-27"


def _player(player_id: int) -> Player:
    return Player.create(
        id=player_id, name=f"P{player_id}", name_normalized=f"p{player_id}",
        espn_id=9000 + player_id, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )


def _snapshot(player_id: int, as_of: date, fpts: int, gp: int = 10) -> None:
    PlayerSeasonStats.create(
        player=player_id, team=None, as_of_date=as_of, season=SEASON, gp=gp, fpts=fpts,
        pts=1, reb=1, ast=1, stl=1, blk=1, tov=1, min=1,
        fgm=1, fga=1, fg3m=1, fg3a=1, ftm=1, fta=1,
    )


def _refreshed() -> dict[int, Rankings]:
    db.execute_sql("REFRESH MATERIALIZED VIEW nba.rankings")
    return {row.id: row for row in Rankings.select()}


@pytest.mark.integration
def test_rank_change_is_measured_against_seven_days_ago(integration_db):
    for pid in (1, 2, 3, 4):
        _player(pid)

    # Ten days back: P4 leads, P1 is last.
    for pid, fpts in ((1, 100), (2, 200), (3, 300), (4, 400)):
        _snapshot(pid, date(2026, 12, 1), fpts)
    # Three days back — inside the window, so it must NOT be the comparison point.
    _snapshot(1, date(2026, 12, 8), 800, gp=12)
    # Today: P1 has surged to first, everyone else slips one place.
    for pid, fpts in ((1, 900), (2, 250), (3, 310), (4, 410)):
        _snapshot(pid, date(2026, 12, 11), fpts, gp=14)

    rows = _refreshed()

    assert rows[1].curr_rank == 1 and rows[1].rank_change == 3     # 4th -> 1st
    assert rows[4].curr_rank == 2 and rows[4].rank_change == -1
    assert rows[3].curr_rank == 3 and rows[3].rank_change == -1
    assert rows[2].curr_rank == 4 and rows[2].rank_change == -1
    assert rows[1].as_of_date == date(2026, 12, 11)


@pytest.mark.integration
def test_a_players_own_snapshot_cadence_does_not_change_the_window(integration_db):
    """The bug 0007 fixes: five snapshots is five *games*, not a fixed span."""
    for pid in (1, 2):
        _player(pid)

    # P1 plays nightly and has many snapshots; P2 has been out and has two.
    # Both are compared against their standing a week ago, not N games ago.
    for day, fpts in ((1, 100), (2, 140), (3, 180), (4, 220), (5, 260), (9, 300), (11, 340)):
        _snapshot(1, date(2026, 12, day), fpts, gp=day)
    _snapshot(2, date(2026, 12, 1), 500)
    _snapshot(2, date(2026, 12, 11), 520, gp=11)

    rows = _refreshed()

    # A week ago (2026-12-04 or earlier): P1 had 220, P2 had 500 -> P2 led.
    # Today: P2 still leads on 520 vs 340. Nobody moved.
    assert rows[2].curr_rank == 1 and rows[1].curr_rank == 2
    assert rows[1].rank_change == 0 and rows[2].rank_change == 0


@pytest.mark.integration
def test_opening_week_reports_no_movement_rather_than_a_fabricated_surge(integration_db):
    """Nothing is older than the cutoff yet, so there is no movement to report."""
    for pid in (1, 2):
        _player(pid)
    _snapshot(1, date(2026, 10, 20), 10, gp=1)
    _snapshot(2, date(2026, 10, 20), 90, gp=1)
    _snapshot(1, date(2026, 10, 24), 500, gp=3)     # a huge swing, inside the window
    _snapshot(2, date(2026, 10, 24), 120, gp=3)

    rows = _refreshed()

    assert rows[1].curr_rank == 1 and rows[2].curr_rank == 2
    assert rows[1].rank_change == 0 and rows[2].rank_change == 0


@pytest.mark.integration
def test_the_copy_can_be_refreshed_without_blocking_readers(integration_db):
    """CONCURRENTLY is what the post-game pipeline uses; it needs rankings_id_key."""
    _player(1)
    _snapshot(1, date(2026, 12, 1), 100)
    db.execute_sql("REFRESH MATERIALIZED VIEW nba.rankings")

    db.execute_sql("REFRESH MATERIALIZED VIEW CONCURRENTLY nba.rankings")

    assert Rankings.select().count() == 1
