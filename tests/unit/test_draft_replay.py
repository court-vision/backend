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
