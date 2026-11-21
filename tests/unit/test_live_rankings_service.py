"""The arithmetic of the public live rankings (services/live_rankings_service.py):
a move between two boards, and how long ESPN has held a rank."""

from datetime import date

import pytest

from services.live_rankings_service import espn_age, move

pytestmark = pytest.mark.unit

D = [date(2026, 11, day) for day in range(17, 22)]      # the 17th to the 21st


def test_a_move_is_places_gained_and_none_without_an_earlier_rank():
    assert move(9, 3) == 6          # ninth to third: up six
    assert move(3, 9) == -6
    assert move(4, 4) == 0
    assert move(None, 4) is None    # new to the board is not "did not move"


def test_espns_rank_is_as_old_as_its_unbroken_run():
    age = espn_age([(D[0], 12), (D[1], 12), (D[2], 9), (D[3], 9), (D[4], 9)], first_stored=D[0])

    assert (age.rank, age.unchanged_since, age.since_first_stored) == (9, D[2], False)


def test_a_rank_it_came_back_to_counts_from_the_return():
    age = espn_age([(D[0], 9), (D[1], 12), (D[2], 9)], first_stored=D[0])

    assert (age.unchanged_since, age.since_first_stored) == (D[2], False)


def test_a_run_that_reaches_the_first_stored_day_is_flagged_as_at_least_that_old():
    age = espn_age([(D[0], 5), (D[1], 5)], first_stored=D[0])

    assert (age.rank, age.unchanged_since, age.since_first_stored) == (5, D[0], True)
    # A morning ESPN's ranks were not stored does not break the run
    assert espn_age([(D[0], 5), (D[3], 5)], first_stored=D[0]).unchanged_since == D[0]
    # He joined ESPN's ranks after they were first stored: the day he joined is known
    assert espn_age([(D[2], 5), (D[3], 5)], first_stored=D[0]).since_first_stored is False


def test_no_rank_has_no_age():
    assert espn_age([], first_stored=D[0]) is None
