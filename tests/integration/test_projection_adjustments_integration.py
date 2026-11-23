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


# ---- notes in season (migration 0052) -------------------------------------------------


def _note(pid=1, **fields):
    fields = {"kind": "injury_current", "return_date": date(2026, 12, 8), "review_date": date(2026, 11, 29),
              "note": "re-evaluated in two weeks", "author": "t", **fields}
    return ProjectionAdjustment.record(pid, SEASON, scope="season", **fields)


def test_a_season_note_sits_beside_the_preseason_adjustment():
    _player()
    _player(2)
    before = ProjectionAdjustment.record(1, SEASON, kind="year2", usage=1.05, note="second-year jump", author="t")
    note = _note(return_earliest=date(2026, 12, 1), return_latest=date(2026, 12, 20), confidence="likely",
                 minutes=24, until_date=date(2026, 12, 22), depends_on_player=2,
                 evidence=[{"type": "news", "id": 7, "headline": "Out two weeks"}])

    # Neither supersedes the other, and each reader gets its own
    assert [a.id for a in ProjectionAdjustment.active_for(SEASON)] == [before.id]
    assert [a.id for a in ProjectionAdjustment.active_for(SEASON, "season")] == [note.id]
    assert ProjectionAdjustment.get_by_id(before.id).superseded_by_id is None
    stored = ProjectionAdjustment.get_by_id(note.id)
    assert (stored.scope, stored.until_date, stored.depends_on_player_id) == ("season", date(2026, 12, 22), 2)
    assert (stored.return_earliest, stored.return_latest, stored.confidence) == (
        date(2026, 12, 1), date(2026, 12, 20), "likely")
    assert stored.evidence == [{"type": "news", "id": 7, "headline": "Out two weeks"}]
    assert before.scope == "preseason" and before.review_date is None


def test_a_new_season_note_supersedes_the_note_and_leaves_the_adjustment():
    _player()
    before = ProjectionAdjustment.record(1, SEASON, kind="role", minutes=30, note="starter", author="t")
    first = _note()
    second = _note(return_date=date(2026, 12, 15))

    assert ProjectionAdjustment.get_by_id(first.id).superseded_by_id == second.id
    assert [a.id for a in ProjectionAdjustment.active_for(SEASON, "season")] == [second.id]
    assert [a.id for a in ProjectionAdjustment.active_for(SEASON)] == [before.id]
    # Retiring is by scope too
    assert ProjectionAdjustment.retire(1, SEASON, "season") == 1
    assert ProjectionAdjustment.active_for(SEASON, "season") == []
    assert [a.id for a in ProjectionAdjustment.active_for(SEASON)] == [before.id]


def test_a_second_live_season_note_is_refused():
    _player()
    _note()
    with pytest.raises(IntegrityError), db.atomic():
        ProjectionAdjustment.insert(player=1, season=SEASON, scope="season", kind="role", minutes=30,
                                    review_date=date(2026, 12, 1), note="y", author="t").execute()


@pytest.mark.parametrize("fields", [
    dict(scope="season", kind="role", minutes=30),                                       # no day to read it again
    dict(scope="season", kind="role", rates={"blk": 1.1}, review_date=date(2026, 12, 1)),
    dict(scope="preseason", kind="role", minutes=30, until_date=date(2026, 12, 1)),
    dict(scope="preseason", kind="role", minutes=30, review_date=date(2026, 12, 1)),
    dict(scope="other", kind="role", minutes=30),
    dict(scope="season", kind="role", minutes=30, review_date=date(2026, 12, 1), confidence="certain"),
    dict(scope="season", kind="role", minutes=30, review_date=date(2026, 12, 1), depends_on_player=1),   # on himself
    # A range is around a date
    dict(scope="season", kind="injury_current", minutes=20, review_date=date(2026, 12, 1),
         return_earliest=date(2026, 12, 1)),
    dict(scope="season", kind="injury_current", return_date=date(2026, 12, 8), review_date=date(2026, 12, 1),
         return_earliest=date(2026, 12, 9)),
    dict(scope="season", kind="injury_current", return_date=date(2026, 12, 8), review_date=date(2026, 12, 1),
         return_latest=date(2026, 12, 7)),
])
def test_constraints_refuse_bad_season_notes(fields):
    _player()
    with pytest.raises(IntegrityError), db.atomic():
        ProjectionAdjustment.insert(player=1, season=SEASON, note="x", author="t", **fields).execute()


def test_a_note_outlives_the_teammate_it_depended_on():
    _player()
    _player(2)
    note = _note(kind="role", return_date=None, minutes=32, depends_on_player=2)
    Player.delete().where(Player.id == 2).execute()
    assert ProjectionAdjustment.get_by_id(note.id).depends_on_player_id is None


def test_player_history_holds_players_outside_nba_players():
    PlayerHistory.create(player_id=2544, season="2012-13", player_name="LeBron James", gp=76, min=2877.0,
                         pts=2036, age=28.0, from_year=2003)
    PlayerHistory.create(player_id=77, season="2025-26", player_name="Nobody", gp=3, min=12.0)
    rows = PlayerHistory.for_seasons(["2012-13"])
    assert [(r.player_id, r.gp, r.from_year) for r in rows] == [(2544, 76, 2003)]
    assert PlayerHistory.for_seasons([]) == []
