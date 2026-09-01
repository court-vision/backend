"""
Draft-session arithmetic and league prefill — the pure half of DraftService.

The DB half (create/list/update, pick resolution) is covered at the API layer
with the service stubbed; what is worth pinning here is the maths a draft room
is wrong about in ways nobody notices: snake reversal, which pick number a new
pick takes after an undo, and how many rounds a league actually drafts.
"""

from types import SimpleNamespace

import pytest

from core.errors import BadRequestError, ConflictError
from services.draft_service import (
    _session_resp,
    league_prefill,
    next_pick_for_slot,
    next_unused_pick,
    round_of,
    resolve_overall_pick,
    rounds_from_roster_slots,
    slot_of,
    total_picks_of,
)

# The real rosterSettings/draftSettings shape, from the captured ESPN payload in
# tests/fixtures/espn_settings_h2h_category.json.
ESPN_ROSTER_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1,
                     "UT": 3, "BE": 3, "IR": 1}
ESPN_DRAFT_SETTINGS = {
    "type": "SNAKE", "date": 1787662080000, "pick_order": [10, 6, 5, 8, 2, 3, 7, 4, 9, 1],
    "time_per_selection": 60, "keeper_count": 0, "auction_budget": 200,
}


def _league(**overrides):
    base = dict(roster_slots=dict(ESPN_ROSTER_SLOTS), draft_settings=dict(ESPN_DRAFT_SETTINGS))
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- snake arithmetic ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("overall,expected_round,expected_slot", [
    (1, 1, 1),
    (10, 1, 10),
    (11, 2, 10),      # the snake turns: slot 10 picks back-to-back
    (20, 2, 1),
    (21, 3, 1),
    (35, 4, 6),       # even round counts down: 10 - ((35-1) % 10) = 6
])
def test_snake_rounds_and_slots(overall, expected_round, expected_slot):
    assert round_of(overall, 10) == expected_round
    assert slot_of(overall, 10) == expected_slot


@pytest.mark.unit
def test_auction_drafts_have_no_round_or_slot():
    assert slot_of(5, 10, "auction") is None
    assert round_of(5, 10, "auction") is None


@pytest.mark.unit
@pytest.mark.parametrize("overall,size", [(0, 10), (-1, 10), (5, 0)])
def test_nonsense_inputs_return_nothing_rather_than_a_wrong_slot(overall, size):
    assert slot_of(overall, size) is None and round_of(overall, size) is None


# ---- pick numbering --------------------------------------------------------


@pytest.mark.unit
def test_the_next_pick_is_the_lowest_unused_one():
    assert next_unused_pick([]) == 1
    assert next_unused_pick([1, 2, 3]) == 4
    # An undo mid-draft leaves a hole, and the hole is what gets refilled — the
    # alternative (max + 1) would silently shift every later pick's slot.
    assert next_unused_pick([1, 2, 4, 5]) == 3


@pytest.mark.unit
def test_my_next_turn_walks_the_snake():
    # Slot 3 of a 10-team snake: picks 3, 18, 23, 38, ...
    assert next_pick_for_slot(1, slot=3, league_size=10) == 3
    assert next_pick_for_slot(4, slot=3, league_size=10) == 18
    assert next_pick_for_slot(19, slot=3, league_size=10) == 23


@pytest.mark.unit
@pytest.mark.parametrize("slot,size,draft_type", [
    (None, 10, "snake"),      # slot not confirmed yet
    (3, None, "snake"),       # no pick order, so no seats
    (3, 10, "auction"),       # auction has no turn order
])
def test_no_turn_to_report_without_a_slot_a_size_and_a_snake(slot, size, draft_type):
    assert next_pick_for_slot(1, slot, size, draft_type) is None


# ---- rounds and prefill ----------------------------------------------------


@pytest.mark.unit
def test_rounds_are_the_draftable_roster_spots():
    # 5 positions + G + F + 3 UT + 3 BE = 13 rounds; IR is filled from the
    # roster, never from the draft board.
    assert rounds_from_roster_slots(ESPN_ROSTER_SLOTS) == 13
    assert rounds_from_roster_slots({}) is None
    assert rounds_from_roster_slots(None) is None
    assert rounds_from_roster_slots({"C": "x"}) is None      # unusable counts are skipped


@pytest.mark.unit
def test_prefill_reads_the_leagues_draft_settings():
    prefill = league_prefill(_league())
    assert prefill["draft_type"] == "snake"
    assert prefill["pick_order"] == [10, 6, 5, 8, 2, 3, 7, 4, 9, 1]
    assert prefill["rounds"] == 13
    assert prefill["keeper_count"] == 0      # an explicit zero is a real answer


@pytest.mark.unit
def test_an_auction_league_prefills_as_an_auction():
    league = _league(draft_settings={**ESPN_DRAFT_SETTINGS, "type": "AUCTION"})
    assert league_prefill(league)["draft_type"] == "auction"


@pytest.mark.unit
def test_espns_other_draft_types_still_draft_in_pick_order():
    for espn_type in ("OFFLINE", "AUTOPICK", None):
        league = _league(draft_settings={**ESPN_DRAFT_SETTINGS, "type": espn_type})
        assert league_prefill(league)["draft_type"] == "snake"


@pytest.mark.unit
def test_no_league_means_no_prefill_rather_than_invented_defaults():
    prefill = league_prefill(None)
    assert prefill == {"draft_type": "snake", "pick_order": [], "rounds": None, "keeper_count": None}


@pytest.mark.unit
def test_an_unsynced_league_prefills_only_what_it_knows():
    prefill = league_prefill(_league(draft_settings={}, roster_slots={}))
    assert prefill["pick_order"] == [] and prefill["rounds"] is None
    assert prefill["keeper_count"] is None and prefill["draft_type"] == "snake"


# ---- which number a new pick takes -----------------------------------------


@pytest.mark.unit
def test_an_omitted_pick_number_takes_the_lowest_unused_one():
    assert resolve_overall_pick([1, 2, 3], None, total_picks=130) == 4
    assert resolve_overall_pick([1, 2, 4], None, total_picks=130) == 3     # the hole an undo left


@pytest.mark.unit
def test_an_explicit_pick_number_is_honoured_and_a_taken_one_is_a_conflict():
    assert resolve_overall_pick([1, 2, 4], 7, total_picks=130) == 7
    with pytest.raises(ConflictError) as exc:
        resolve_overall_pick([1, 2, 4], 2, total_picks=130)
    assert exc.value.error_code == "DRAFT_PICK_ALREADY_EXISTS"


@pytest.mark.unit
def test_a_pick_past_the_end_of_the_draft_is_rejected():
    with pytest.raises(BadRequestError) as exc:
        resolve_overall_pick([], 131, total_picks=130)
    assert exc.value.error_code == "DRAFT_PICK_OUT_OF_RANGE"
    # ...and with no known length there is nothing to be past.
    assert resolve_overall_pick([], 131, total_picks=None) == 131


@pytest.mark.unit
def test_the_draft_length_needs_both_a_size_and_a_round_count():
    session = SimpleNamespace(pick_order=[1, 2, 3, 4], rounds=13)
    assert total_picks_of(session) == 52
    # A session nobody sized (a bare mock draft) has no end to run past.
    assert total_picks_of(SimpleNamespace(pick_order=[], rounds=13)) is None
    assert total_picks_of(SimpleNamespace(pick_order=[1, 2], rounds=None)) is None


# ---- the session response's view of the draft ------------------------------


def _session(**overrides):
    base = dict(
        id=12, team_id=7, league_id=3, kind="manual", status="active", draft_type="snake",
        pick_order=[10, 6, 5, 8, 2, 3, 7, 4, 9, 1], my_slot=3, rounds=13, keepers=[],
        started_at=None, completed_at=None, created_at=None, updated_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_the_session_reports_the_draft_size_and_length():
    resp = _session_resp(_session(), used_picks=[], keeper_count=0)
    assert resp.league_size == 10 and resp.total_picks == 130
    assert resp.pick_count == 0 and resp.next_overall_pick == 1
    assert resp.my_next_pick == 3 and resp.picks_until_my_turn == 2
    assert resp.keeper_count == 0


@pytest.mark.unit
def test_my_turn_is_counted_from_the_draft_front_not_from_an_undos_hole():
    """After undoing pick 5 of a 20-pick draft the room is still at pick 21;
    only the number a correction would reuse went backwards."""
    used = [p for p in range(1, 21) if p != 5]
    resp = _session_resp(_session(), used_picks=used, keeper_count=0)

    assert resp.next_overall_pick == 5            # the hole a correction refills
    assert resp.my_next_pick == 23                # slot 3's next turn after pick 20
    assert resp.picks_until_my_turn == 2          # measured from pick 21, the front
    assert resp.pick_count == 19


@pytest.mark.unit
def test_a_session_without_a_pick_order_reports_no_seats_rather_than_guessing():
    resp = _session_resp(_session(pick_order=[], my_slot=None), used_picks=[1, 2], keeper_count=None)
    assert resp.league_size is None and resp.total_picks is None
    assert resp.my_next_pick is None and resp.picks_until_my_turn is None
    assert resp.next_overall_pick == 3
