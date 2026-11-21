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
    age = espn_age([(D[0], 12), (D[1], 12), (D[2], 9), (D[3], 9), (D[4], 9)], stored=D)

    assert (age.rank, age.unchanged_since, age.since_first_stored) == (9, D[2], False)


def test_a_rank_it_came_back_to_counts_from_the_return():
    age = espn_age([(D[0], 9), (D[1], 12), (D[2], 9)], stored=D[:3])

    assert (age.unchanged_since, age.since_first_stored) == (D[2], False)


def test_a_day_espn_stored_a_board_without_him_ends_the_run():
    # Tenth, off ESPN's board the next morning, tenth again: held since the return, not since the first day
    age = espn_age([(D[0], 10), (D[2], 10), (D[3], 10)], stored=D[:4])

    assert (age.rank, age.unchanged_since, age.since_first_stored) == (10, D[2], False)


def test_a_morning_espn_stored_nothing_does_not_end_the_run():
    age = espn_age([(D[0], 5), (D[3], 5)], stored=[D[0], D[3]])

    assert (age.unchanged_since, age.since_first_stored) == (D[0], True)


def test_a_run_that_reaches_the_first_stored_day_is_flagged_as_at_least_that_old():
    age = espn_age([(D[0], 5), (D[1], 5)], stored=D[:2])

    assert (age.rank, age.unchanged_since, age.since_first_stored) == (5, D[0], True)
    # He joined ESPN's ranks after they were first stored: the day he joined is known
    assert espn_age([(D[2], 5), (D[3], 5)], stored=D[:4]).since_first_stored is False


def test_the_age_is_counted_up_to_his_newest_row_and_his_own_days_count_as_stored():
    # A stored day after his newest row is not part of the run (the caller cuts his rows at the read it shows)
    age = espn_age([(D[1], 7), (D[2], 7)], stored=[D[1], D[2], D[4]])
    assert (age.rank, age.unchanged_since, age.since_first_stored) == (7, D[1], True)
    # A list of stored days that misses one of his own still counts it
    assert espn_age([(D[0], 7), (D[1], 7)], stored=[D[1]]).unchanged_since == D[0]


def test_no_rank_has_no_age():
    assert espn_age([], stored=D) is None
