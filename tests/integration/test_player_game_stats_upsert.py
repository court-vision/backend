"""
Upserting a box-score line, against the real constraints.

Migration 0019 moved the row's identity from the date to the game and left the
date as a partial unique index over rows that have no game. The upsert has to
key the same way, and the interesting cases are the seams between the two: an
id the schedule has not caught up with, and a row written before its id was
known.
"""

from datetime import date, datetime

import pytest

from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam

pytestmark = pytest.mark.integration

DAY = date(2026, 11, 1)
STATS = {"fpts": 40, "pts": 30, "reb": 8, "ast": 5, "stl": 1, "blk": 1, "tov": 3,
         "min": 34, "fgm": 11, "fga": 20, "fg3m": 3, "fg3a": 7, "ftm": 5, "fta": 6}


@pytest.fixture
def fixture(integration_db):
    # nba.teams is a dimension table and is not truncated between tests.
    for abbrev, name in (("LAL", "Lakers"), ("BOS", "Celtics")):
        NBATeam.get_or_create(
            id=abbrev,
            defaults={"name": name, "city": abbrev, "conference": "West",
                      "division": "Pacific"},
        )
    Player.create(id=201, name="A Player", name_normalized="a player")
    Game.create(game_id="0022600001", game_date=DAY, season="2026-27",
                home_team="LAL", away_team="BOS", status="final")
    return None


def rows_for(player_id=201):
    return list(PlayerGameStats.select().where(PlayerGameStats.player == player_id))


def test_a_known_game_keys_the_row(fixture):
    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL", game_id="0022600001")
    (row,) = rows_for()
    assert row.game_id == "0022600001" and row.game_date == DAY


def test_the_same_game_twice_updates_rather_than_duplicates(fixture):
    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL", game_id="0022600001")
    PlayerGameStats.upsert_game_stats(201, DAY, {**STATS, "pts": 33}, team_id="LAL",
                                      game_id="0022600001")
    (row,) = rows_for()
    assert row.pts == 33


def test_an_id_the_schedule_has_not_seen_falls_back_to_the_date(fixture):
    # The column is a foreign key: storing an unknown id would fail the whole
    # row. A game the schedule pipeline has not reached yet must not cost us
    # the box score.
    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL", game_id="0022699999")
    (row,) = rows_for()
    assert row.game_id is None and row.game_date == DAY


def test_a_row_written_without_an_id_is_promoted_rather_than_duplicated(fixture):
    # The transition case, and the one both unique indexes allow through:
    # (player, NULL) and (player, game) are distinct to Postgres, so a second
    # insert would be accepted and the line stored twice.
    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL")
    assert rows_for()[0].game_id is None

    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL", game_id="0022600001")
    (row,) = rows_for()
    assert row.game_id == "0022600001"


def test_a_game_whose_date_was_corrected_moves_its_row(fixture):
    PlayerGameStats.upsert_game_stats(201, DAY, STATS, team_id="LAL", game_id="0022600001")
    moved = date(2026, 11, 2)
    Game.update(game_date=moved).where(Game.game_id == "0022600001").execute()

    PlayerGameStats.upsert_game_stats(201, moved, STATS, team_id="LAL", game_id="0022600001")

    (row,) = rows_for()
    assert row.game_date == moved, "the row followed its game rather than forking"


def _commit_competing_row(day):
    """Write the same (player, game) on a second connection and commit it."""
    import os
    from playhouse.db_url import connect as connect_url

    other = connect_url(os.environ["DATABASE_URL"])
    other.execute_sql(
        "INSERT INTO nba.player_game_stats "
        "(player_id, team_id, game_date, game_id, fpts, pts, reb, ast, stl, blk, tov, min,"
        " fgm, fga, fg3m, fg3a, ftm, fta, created_at, updated_at) "
        "VALUES (201, 'LAL', %s, '0022600001', 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,"
        " now(), now())",
        (day,),
    )
    other.commit()
    other.close()


def test_a_racing_writer_reloads_rather_than_failing(fixture, monkeypatch):
    """The lookup is advisory; the unique index decides.

    Two writers that both find nothing will both insert and one will lose. The
    loser has to re-read and update rather than raise — which is what peewee's
    `get_or_create` did before this upsert was written by hand.

    The competing row lands *between* our lookup and our insert, on a second
    connection so it is genuinely committed rather than sharing (and dying with)
    our transaction. Both halves matter: commit it earlier and the first lookup
    simply finds it, which is a different path entirely.
    """
    real_create = PlayerGameStats.create.__func__

    def create_after_losing_the_race(cls, **kwargs):
        monkeypatch.undo()          # only race the first insert
        _commit_competing_row(DAY)
        return real_create(cls, **kwargs)

    monkeypatch.setattr(PlayerGameStats, "create",
                        classmethod(create_after_losing_the_race))

    row = PlayerGameStats.upsert_game_stats(
        201, DAY, {**STATS, "pts": 41}, team_id="LAL", game_id="0022600001"
    )

    assert row.pts == 41, "the losing writer's values should still land"
    assert len(rows_for()) == 1, "and there should be exactly one row"
