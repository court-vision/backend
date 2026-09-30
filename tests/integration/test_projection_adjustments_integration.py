"""
nba.projection_adjustments and nba.player_history against the real migration.

The supersede path is the one the unique index and the deferred foreign key
were designed together for, so it is exercised here rather than trusted: an
edit must leave exactly one live row, with the old one pointing at it.
"""

from datetime import date

import pytest
from peewee import IntegrityError

from db.base import db
from db.models.nba import Player, PlayerHistory, ProjectionAdjustment

pytestmark = pytest.mark.integration

SEASON = "2026-27"


def _player(pid=1):
    return Player.create(id=pid, name=f"P{pid}", name_normalized=f"p{pid}")


def test_record_supersedes_and_keeps_one_live_row():
    _player()
    first = ProjectionAdjustment.record(1, SEASON, kind="trade", minutes=30, note="new team", author="t")
    second = ProjectionAdjustment.record(1, SEASON, kind="trade", minutes=32, note="starter", author="t")

    live = ProjectionAdjustment.active_for(SEASON)
    assert [a.id for a in live] == [second.id]
    assert ProjectionAdjustment.get_by_id(first.id).superseded_by_id == second.id
    assert float(second.minutes) == 32.0


def test_a_second_live_row_is_refused():
    _player()
    ProjectionAdjustment.record(1, SEASON, kind="role", usage=1.1, note="x", author="t")
    with pytest.raises(IntegrityError), db.atomic():
        ProjectionAdjustment.insert(player=1, season=SEASON, kind="role", usage=1.2, note="y", author="t").execute()


def test_retire_leaves_none_live_and_the_history_kept():
    _player()
    ProjectionAdjustment.record(1, SEASON, kind="injury_return", return_date=date(2026, 12, 1),
                                note="Achilles", author="t")
    assert ProjectionAdjustment.retire(1, SEASON) == 1
    assert ProjectionAdjustment.active_for(SEASON) == []
    assert ProjectionAdjustment.select().count() == 1
    # A new judgment after a retirement is a fresh live row.
    ProjectionAdjustment.record(1, SEASON, kind="injury_return", games=40, note="back", author="t")
    assert len(ProjectionAdjustment.active_for(SEASON)) == 1


@pytest.mark.parametrize("fields", [
    dict(kind="nonsense", minutes=30),
    dict(kind="role"),                         # changes nothing
    dict(kind="role", minutes=60),
    dict(kind="role", games=90),
    dict(kind="role", usage=0),
])
def test_constraints_refuse_bad_rows(fields):
    _player()
    with pytest.raises(IntegrityError), db.atomic():
        ProjectionAdjustment.insert(player=1, season=SEASON, note="x", author="t", **fields).execute()


def test_seasons_are_independent():
    _player()
    ProjectionAdjustment.record(1, "2025-26", kind="role", minutes=20, note="old", author="t")
    ProjectionAdjustment.record(1, SEASON, kind="role", minutes=30, note="new", author="t")
    assert [float(a.minutes) for a in ProjectionAdjustment.active_for(SEASON)] == [30.0]


def test_player_history_holds_players_outside_nba_players():
    PlayerHistory.create(player_id=2544, season="2012-13", player_name="LeBron James", gp=76, min=2877.0,
                         pts=2036, age=28.0, from_year=2003)
    PlayerHistory.create(player_id=77, season="2025-26", player_name="Nobody", gp=3, min=12.0)
    rows = PlayerHistory.for_seasons(["2012-13"])
    assert [(r.player_id, r.gp, r.from_year) for r in rows] == [(2544, 76, 2003)]
    assert PlayerHistory.for_seasons([]) == []
