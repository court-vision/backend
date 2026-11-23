"""What stands beside a Draft lab board in season (services/draft_live.py): the
moves between two stored boards, ESPN's rank that morning and how long it has
stood, and the one line of reason read off the live projection."""

from datetime import date

import pytest

from services.draft_live import EspnNow, espn_now, moves_of, near, reason_of

pytestmark = pytest.mark.unit

D = [date(2026, 11, day) for day in range(17, 24)]      # the 17th to the 23rd
TODAY = D[-1]


def test_a_stored_board_is_read_only_when_it_is_near_enough():
    assert near(D[6], TODAY) == D[6]
    assert near(D[3], TODAY) == D[3]            # three days back: the morning job missed a weekend
    assert near(D[2], TODAY) is None            # four: it has been failing, and a move off it is last week's
    assert near(date(2026, 11, 24), TODAY) is None
    assert near(None, TODAY) is None


def test_a_move_is_places_gained_and_none_where_he_was_not_ranked_then():
    moves = moves_of({1: 3, 2: 10, 3: 7}, day={1: 4, 2: 8}, week={1: 9})

    assert moves == {1: (1, 6), 2: (-2, None), 3: (None, None)}


def test_espns_rank_is_the_mornings_with_the_day_it_became_that():
    rows = [
        (1, D[0], 12, 30.0), (1, D[1], 9, 31.0), (1, D[2], 9, 32.0),     # moved on the 18th
        (2, D[0], 5, 50.0), (2, D[1], 5, 50.0), (2, D[2], 5, None),      # never moved
        (3, D[0], 40, 2.0), (3, D[1], 41, 2.0),                          # off its board by the 19th
    ]

    now = espn_now(rows, stored=D[:3], as_of=D[2])

    assert now[1] == EspnNow(rank=9, value=32.0, since=D[1], since_first_stored=False)
    # Held since the first morning its ranks were stored: at least that old
    assert now[2] == EspnNow(rank=5, value=None, since=D[0], since_first_stored=True)
    # A rank ESPN no longer gives him is not his rank
    assert 3 not in now


def test_a_read_newer_than_the_morning_shown_is_not_counted():
    rows = [(1, D[0], 12, None), (1, D[1], 12, None), (1, D[2], 3, None)]

    now = espn_now(rows, stored=D[:3], as_of=D[1])

    assert (now[1].rank, now[1].since) == (12, D[0])


def test_an_absence_is_the_reason_before_anything_else():
    hurt = {"known": {"return_date": "2026-12-08", "out_next": True}, "recent_minutes": 20.0, "season_minutes": 30.0}

    assert reason_of(hurt, TODAY, moved=-9) == "Out until about Dec 8"
    assert reason_of({"known": {"out_for_season": True, "note": 41}}, TODAY) == "Out for the season (from a note)"
    assert reason_of({"known": {"return_date": "2026-12-08", "note": 41}}, TODAY) == (
        "Out until about Dec 8 (from a note)")
    # A return date that has come is no longer a reason
    assert reason_of({"known": {"return_date": "2026-11-23"}}, TODAY) is None
    assert reason_of({"known": {"out_next": True}}, TODAY) == "Out of his team's next game"


def test_a_note_in_force_says_what_it_sets_and_how_long_it_holds():
    names = {7: "Big Man"}

    assert reason_of({"note": {"id": 3, "minutes": 32.0, "depends_on": 7, "through": "2026-12-01"}},
                     TODAY, names=names) == "Note: 32 minutes a game while Big Man is out"
    assert reason_of({"note": {"id": 3, "minutes": 28.5, "until": "2026-12-05"}}, TODAY) == (
        "Note: 28.5 minutes a game through Dec 5")
    assert reason_of({"note": {"id": 3, "usage": 1.15, "through": "2026-11-30"}}, TODAY) == (
        "Note: a bigger share of the offense through Nov 30")
    assert reason_of({"note": {"id": 3, "usage": 0.9}}, TODAY) == "Note: a smaller share of the offense"
    # A note that sets neither is no reason
    assert reason_of({"note": {"id": 3}}, TODAY) is None


def test_minutes_are_the_reason_only_for_a_plain_move_the_same_way():
    up = {"recent_minutes": 34.2, "season_minutes": 29.5}

    assert reason_of(up, TODAY, moved=8) == "Minutes up: 34.2 lately, 29.5 on the season"
    assert reason_of({"recent_minutes": 22.0, "season_minutes": 27.4}, TODAY, moved=-6) == (
        "Minutes down: 22.0 lately, 27.4 on the season")
    assert reason_of(up, TODAY, moved=4) is None            # a small move needs no reason
    assert reason_of(up, TODAY, moved=-8) is None           # more minutes do not explain a fall
    assert reason_of(up, TODAY) is None                     # no board to measure a move against
    assert reason_of({"recent_minutes": 30.0, "season_minutes": 29.5}, TODAY, moved=8) is None


def test_no_reason_without_what_the_projection_read():
    assert reason_of(None, TODAY, moved=12) is None
    assert reason_of({}, TODAY, moved=12) is None
    assert reason_of({"known": "garbled", "note": 5, "recent_minutes": "x"}, TODAY, moved=12) is None
