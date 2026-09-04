"""
The snake arithmetic, checked against a real draft rather than against itself.

`tests/fixtures/draft_replay_4team_13round.json` is a genuine ESPN snake draft
captured pick by pick: for all 52 picks it records which seat was actually on
the clock. That makes it the one test here that cannot be wrong in the same
direction as the code — a hand-written expectation reproduces whatever the
author believed, this reproduces what ESPN did.

The end-to-end replay of the same capture (sessions, board, undo, keepers)
lives in tests/integration/test_draft_replay_integration.py.
"""

import pytest

from services.draft_service import pick_for_slot, round_of, slot_of

pytestmark = pytest.mark.unit


def test_the_capture_is_a_complete_snake_draft(replay):
    """Guard the fixture itself: a hole or a stray team would quietly weaken
    every assertion below."""
    picks = replay["picks"]
    size, rounds = replay["league_size"], replay["rounds"]

    assert [p["overall"] for p in picks] == list(range(1, size * rounds + 1))
    assert sorted(replay["pick_order"]) == sorted({p["team"] for p in picks})
    # Every seat drafts exactly once per round.
    for team in replay["pick_order"]:
        assert sorted(p["round"] for p in picks if p["team"] == team) == list(range(1, rounds + 1))


def test_our_slot_arithmetic_reproduces_who_was_on_the_clock(replay):
    """For all 52 picks: the seat `slot_of` computes, resolved through the
    league's own pick order, is the team ESPN actually gave that pick to."""
    order, size = replay["pick_order"], replay["league_size"]

    for pick in replay["picks"]:
        overall = pick["overall"]
        slot = slot_of(overall, size)
        assert slot is not None, overall
        assert order[slot - 1] == pick["team"], (
            f"pick {overall}: we say seat {slot} (team {order[slot - 1]}), ESPN gave it to team {pick['team']}"
        )
        assert round_of(overall, size) == pick["round"], overall


def test_the_snake_actually_turns_in_this_capture(replay):
    """The check above would pass on a non-snake draft too if the capture never
    reversed. It does: round two runs backwards through the same seats."""
    order, size = replay["pick_order"], replay["league_size"]
    by_overall = {p["overall"]: p["team"] for p in replay["picks"]}

    first_round = [by_overall[n] for n in range(1, size + 1)]
    second_round = [by_overall[n] for n in range(size + 1, 2 * size + 1)]

    assert first_round == order
    assert second_round == list(reversed(order))


def test_pricing_a_round_back_to_a_pick_round_trips(replay):
    """`pick_for_slot` is what a keeper costs. It must invert `slot_of` exactly,
    or a keeper would be recorded on a pick its seat never owned."""
    order, size = replay["pick_order"], replay["league_size"]
    seat_of = {team: i + 1 for i, team in enumerate(order)}

    for pick in replay["picks"]:
        seat = seat_of[pick["team"]]
        assert pick_for_slot(pick["round"], seat, size) == pick["overall"]


# ---- what the board asks availability about --------------------------------


def _geometry(replay, made, seat=3, keepers=()):
    """The board's view of where this draft has got to, after `made` picks."""
    from services.draft_board_service import BoardInputs, BoardSession, DraftBoardService

    return DraftBoardService._with_geometry(
        BoardSession(session_id=1, my_slot=seat, league_size=replay["league_size"],
                     rounds=replay["rounds"], draft_type=replay["draft_type"]),
        BoardInputs(season="2026-27", pool=[],
                    used_picks=tuple(made), keeper_picks=tuple(keepers)),
    )


def test_the_board_counts_my_next_turns_to_the_seats_espn_actually_used(replay):
    """Availability is only as good as the pick it is asked about. Seat 3 has
    just picked (3 of 52 made): the front is 4, and the next two turns the
    board will ask about are the picks ESPN gave that same team."""
    order, seat = replay["pick_order"], 3
    my_team = order[seat - 1]
    by_overall = {p["overall"]: p["team"] for p in replay["picks"]}

    session = _geometry(replay, made=[1, 2, 3], seat=seat)

    assert session.draft_front == 4
    assert session.my_next_pick == 6 and session.my_following_pick == 11
    assert by_overall[session.my_next_pick] == my_team
    assert by_overall[session.my_following_pick] == my_team


def test_the_availability_bands_sit_half_a_round_either_side_of_that_turn(replay):
    from services.draft_board_service import DraftBoardService

    session = _geometry(replay, made=[1, 2, 3])
    horizon = DraftBoardService._availability_horizon(session)
    assert horizon == 6

    def bucket(adp):
        return DraftBoardService._availability_of(
            {"adp": adp}, horizon, replay["league_size"]
        )

    # Four teams, so the band is the floor of three picks, not two.
    assert bucket(9.0) == "likely" and bucket(8.9) == "tossup"
    assert bucket(3.0) == "gone" and bucket(3.1) == "tossup"
    # No crowd ADP falls back to ESPN's editorial rank; no market data at all
    # is silence rather than a guess.
    assert DraftBoardService._availability_of({"overall_rank": 40}, horizon, 4) == "likely"
    assert DraftBoardService._availability_of({"adp": None, "overall_rank": 1}, horizon, 4) == "gone"
    assert DraftBoardService._availability_of({}, horizon, 4) is None


def test_on_the_clock_the_horizon_moves_to_the_turn_after_this_one(replay):
    """With five picks made seat 3 is on the clock at 6. Asking whether a
    player survives to 6 is asking whether he survives to now."""
    from services.draft_board_service import DraftBoardService

    session = _geometry(replay, made=[1, 2, 3, 4, 5])

    assert session.draft_front == 6 and session.my_next_pick == 6
    assert DraftBoardService._availability_horizon(session) == 11


def test_a_keeper_is_not_a_turn_on_the_clock(replay):
    """Seat 3's second-round pick is spent on a keeper before the draft opens:
    the next turn it can actually use is the third-round one."""
    session = _geometry(replay, made=[1, 2, 3, 6], keepers=[6])

    assert session.draft_front == 4
    assert session.my_next_pick == 11


def test_a_finished_draft_asks_about_nothing(replay):
    """Past the last pick there is no next turn, which makes availability
    meaningless rather than urgent."""
    from services.draft_board_service import DraftBoardService

    every_pick = [p["overall"] for p in replay["picks"]]
    session = _geometry(replay, made=every_pick)

    assert session.my_next_pick is None
    assert DraftBoardService._availability_horizon(session) is None
