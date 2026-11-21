"""The public read of the stored daily boards (services/live_rankings_service.py)
over a real database: nba.daily_rankings as the data platform writes it.

The boards here are tiny (four players) and written by hand, a day at a time,
so every rank, move and date in the answers can be read off the test.
"""

from datetime import date

import pytest

from core.errors import NotFoundError
from core.settings import settings
from db.base import db
from db.models.nba.daily_rankings import (
    BOARD_CATEGORIES,
    BOARD_POINTS,
    SOURCE_CV,
    SOURCE_ESPN,
    DailyRanking,
)
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from services.live_rankings_service import LiveRankingsService

pytestmark = pytest.mark.integration

SEASON = "2026-27"
TODAY = date(2026, 11, 21)
YESTERDAY = date(2026, 11, 20)
WEEK_AGO = date(2026, 11, 14)
FIRST = date(2026, 11, 12)
STAR, GUARD, WING, BIG = 930_001, 930_002, 930_003, 930_004
SHADOW = "cv_shadow"    # a candidate board graded in shadow: stored beside the plain one, never served

board = LiveRankingsService.get_board.__wrapped__       # the sync bodies, without the run_db boundary
sheet = LiveRankingsService.get_player.__wrapped__


def store(day, source, which, ranks, season=SEASON, **facts):
    """One board of one source for one day: `ranks` is {player_id: rank} or {player_id: (rank, value)}."""
    rows = []
    for player_id, rank in ranks.items():
        rank, value = rank if isinstance(rank, tuple) else (rank, None)
        rows.append({"player_id": player_id, "rank": rank, "value": value, **facts.get(str(player_id), {})})
    with db.atomic():
        DailyRanking.replace_day(day, season, source, which, rows)


@pytest.fixture
def players(integration_db, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", SEASON, raising=False)
    for pid, name, position in ((STAR, "Steady Star", "C"), (GUARD, "Rising Guard", "G"),
                                (WING, "Falling Wing", "F"), (BIG, "New Big", None)):
        Player.create(id=pid, espn_id=pid - 900_000, name=name, name_normalized=name.lower(), position=position)
    NBATeam.get_or_create(id="DEN", defaults={"name": "Denver Nuggets", "conference": "West", "division": "Northwest"})
    PlayerProfile.create(player=STAR, team="DEN")
    PlayerProfile.create(player=GUARD, team=None)


def a_week_of_boards():
    """Points boards a week ago, yesterday and today; 9-cat today; ESPN today; a shadow board today."""
    store(WEEK_AGO, SOURCE_CV, BOARD_POINTS, {STAR: 1, WING: 2, GUARD: 3})
    store(YESTERDAY, SOURCE_CV, BOARD_POINTS, {STAR: 1, GUARD: 2, WING: 3})
    store(TODAY, SOURCE_CV, BOARD_POINTS, {GUARD: (1, 48.25), STAR: (2, 51.5), WING: (3, 30.0), BIG: (4, 22.0)},
          **{str(STAR): {"games": 58.5, "season_gp": 12, "injury_status": "DTD", "model": "live-1"},
             str(GUARD): {"games": 61.0, "season_gp": 14, "model": "live-1"}})
    store(TODAY, SOURCE_CV, BOARD_CATEGORIES, {STAR: (1, 1.42), GUARD: (2, 0.9), WING: (3, 0.1), BIG: (4, -0.3)})
    store(TODAY, SOURCE_ESPN, BOARD_POINTS, {STAR: 1, WING: 2, GUARD: 9})
    store(TODAY, SOURCE_ESPN, BOARD_CATEGORIES, {STAR: 3})
    # What must never be read: the shadow board, with everyone reversed
    store(TODAY, SHADOW, BOARD_POINTS, {BIG: 1, WING: 2, STAR: 3, GUARD: 4})


def test_before_the_first_board_the_answer_is_a_successful_empty_one(players):
    # Last season's boards are not this season's, and a shadow board is not a board
    store(date(2026, 4, 10), SOURCE_CV, BOARD_POINTS, {WING: 1}, season="2025-26")
    store(TODAY, SHADOW, BOARD_POINTS, {BIG: 1})

    data = board().data

    assert (data.season, data.as_of, data.model, data.espn_as_of) == (SEASON, None, None, None)
    assert data.players == [] and (data.compared.day, data.compared.week) == (None, None)


def test_the_board_is_this_mornings_with_each_move_and_espn_beside_it(players):
    a_week_of_boards()

    data = board().data

    assert (data.season, data.as_of, data.model) == (SEASON, TODAY, "live-1")
    assert (data.compared.day, data.compared.week, data.espn_as_of) == (YESTERDAY, WEEK_AGO, TODAY)
    # In points-board order, whatever the shadow board says
    assert [p.player_id for p in data.players] == [GUARD, STAR, WING, BIG]
    guard, star, wing, big = data.players

    # Up one since yesterday and two since last week; ESPN still has him ninth
    assert guard.points.model_dump() == {"rank": 1, "value": 48.25, "move_1d": 1, "move_7d": 2, "espn_rank": 9}
    assert star.points.model_dump() == {"rank": 2, "value": 51.5, "move_1d": -1, "move_7d": -1, "espn_rank": 1}
    assert (wing.points.move_1d, wing.points.move_7d) == (0, -1)
    # New to the board: no earlier rank to move from, and ESPN does not rank him
    assert (big.points.move_1d, big.points.move_7d, big.points.espn_rank) == (None, None, None)
    # The 9-cat board has no earlier day: a rank, ESPN's ROTO rank beside it, and no move
    assert star.categories.model_dump() == {"rank": 1, "value": 1.42, "move_1d": None, "move_7d": None, "espn_rank": 3}
    assert guard.categories.espn_rank is None

    # What the board knew about him, and where he plays now
    assert (star.name, star.team, star.position) == ("Steady Star", "DEN", "C")
    assert (star.games, star.season_gp, star.injury_status) == (58.5, 12, "DTD")
    assert (guard.team, guard.injury_status, big.team, big.position) == (None, None, None, None)


def test_a_board_too_far_back_is_not_called_yesterday_or_last_week(players):
    store(date(2026, 11, 10), SOURCE_CV, BOARD_POINTS, {STAR: 2, GUARD: 1})    # eleven days back
    store(date(2026, 11, 17), SOURCE_CV, BOARD_POINTS, {STAR: 2, GUARD: 1})    # four days back
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1, GUARD: 2})

    data = board().data

    assert (data.compared.day, data.compared.week) == (None, None)
    assert [(p.points.move_1d, p.points.move_7d) for p in data.players] == [(None, None), (None, None)]


def test_the_day_before_can_be_three_days_back_and_the_week_ten(players):
    store(date(2026, 11, 11), SOURCE_CV, BOARD_POINTS, {STAR: 2, GUARD: 1})    # ten days back
    store(date(2026, 11, 18), SOURCE_CV, BOARD_POINTS, {STAR: 2, GUARD: 1})    # three days back
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1, GUARD: 2})

    data = board().data

    assert (data.compared.day, data.compared.week) == (date(2026, 11, 18), date(2026, 11, 11))
    assert [(p.points.move_1d, p.points.move_7d) for p in data.players] == [(1, 1), (-1, -1)]


def test_espns_ranks_are_the_newest_read_when_this_mornings_failed(players):
    store(YESTERDAY, SOURCE_ESPN, BOARD_POINTS, {STAR: 4})
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1})

    data = board().data

    assert (data.as_of, data.espn_as_of) == (TODAY, YESTERDAY)
    assert data.players[0].points.espn_rank == 4


def test_a_players_sheet_has_todays_places_his_history_and_how_long_espn_has_stood(players):
    a_week_of_boards()
    # ESPN's points rank for the guard: 12 on the first stored day, 9 since the 19th. The star's never moved.
    store(FIRST, SOURCE_ESPN, BOARD_POINTS, {GUARD: 12, STAR: 1})
    store(date(2026, 11, 19), SOURCE_ESPN, BOARD_POINTS, {GUARD: 9, STAR: 1})
    store(YESTERDAY, SOURCE_ESPN, BOARD_POINTS, {GUARD: 9, STAR: 1})

    guard = sheet(GUARD).data

    assert (guard.player_id, guard.name, guard.position, guard.team) == (GUARD, "Rising Guard", "G", None)
    assert (guard.season, guard.as_of, guard.model) == (SEASON, TODAY, "live-1")
    assert (guard.compared.day, guard.compared.week) == (YESTERDAY, WEEK_AGO)
    assert guard.points.model_dump() == {"rank": 1, "value": 48.25, "move_1d": 1, "move_7d": 2, "espn_rank": 9}
    assert guard.categories.rank == 2 and guard.categories.espn_rank is None
    assert (guard.games, guard.season_gp, guard.injury_status) == (61.0, 14, None)
    assert guard.espn.as_of == TODAY
    assert guard.espn.points.model_dump() == {
        "rank": 9, "unchanged_since": date(2026, 11, 19), "since_first_stored": False,
    }
    assert guard.espn.categories is None
    # Oldest first, one entry a stored day, and never a rank from the shadow board (which has him fourth)
    assert [day.model_dump() for day in guard.history] == [
        {"as_of": FIRST, "points": None, "categories": None, "espn_points": 12, "espn_categories": None},
        {"as_of": WEEK_AGO, "points": 3, "categories": None, "espn_points": None, "espn_categories": None},
        {"as_of": date(2026, 11, 19), "points": None, "categories": None, "espn_points": 9, "espn_categories": None},
        {"as_of": YESTERDAY, "points": 2, "categories": None, "espn_points": 9, "espn_categories": None},
        {"as_of": TODAY, "points": 1, "categories": 2, "espn_points": 9, "espn_categories": None},
    ]

    star = sheet(STAR).data
    # ESPN has held him first since its ranks were first stored: the rank is at least that old
    assert star.espn.points.model_dump() == {"rank": 1, "unchanged_since": FIRST, "since_first_stored": True}
    assert star.espn.categories.model_dump() == {"rank": 3, "unchanged_since": TODAY, "since_first_stored": False}
    assert star.team == "DEN"


def test_a_player_off_this_mornings_board_keeps_his_history(players):
    store(YESTERDAY, SOURCE_CV, BOARD_POINTS, {WING: 3, STAR: 1})
    store(YESTERDAY, SOURCE_ESPN, BOARD_POINTS, {WING: 40})
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1})
    store(TODAY, SOURCE_ESPN, BOARD_POINTS, {STAR: 1})

    wing = sheet(WING).data

    assert (wing.as_of, wing.points, wing.categories) == (TODAY, None, None)
    assert (wing.games, wing.injury_status, wing.model) == (None, None, None)
    # ESPN dropped him too: yesterday's rank is history, not his rank now
    assert (wing.espn.as_of, wing.espn.points) == (TODAY, None)
    assert [(day.as_of, day.points, day.espn_points) for day in wing.history] == [(YESTERDAY, 3, 40)]


def test_a_player_never_ranked_this_season_has_an_empty_sheet(players):
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1})
    store(TODAY, SHADOW, BOARD_POINTS, {BIG: 1})        # only the shadow board has him

    big = sheet(BIG).data

    assert (big.player_id, big.name, big.as_of) == (BIG, "New Big", TODAY)
    assert big.points is None and big.history == [] and big.espn.points is None


def test_an_unknown_player_is_not_found(players):
    with pytest.raises(NotFoundError):
        sheet(999_999_999)
