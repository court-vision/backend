"""
Draft-session arithmetic and league prefill — the pure half of DraftService.

The DB half (create/list/update, pick resolution) is covered at the API layer
with the service stubbed and against a real schema in
tests/integration/test_draft_sessions_integration.py; what is worth pinning
here is the maths a draft room is wrong about in ways nobody notices: snake
reversal, which pick number a new pick takes after an undo, how many rounds a
league actually drafts, and when two picks name the same player.
"""

from types import SimpleNamespace

import pytest

from peewee import IntegrityError

from core.errors import BadRequestError, ConflictError
from services.draft_service import (
    DraftService,
    _session_resp,
    check_length_holds_picks,
    check_slot_in_range,
    draft_front,
    duplicate_pick_of,
    league_prefill,
    next_pick_for_slot,
    matching_keeper,
    next_unused_pick,
    pick_for_slot,
    pick_geometry,
    plan_keeper_moves,
    resolve_lagging_picks,
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


# ---- one player, one pick --------------------------------------------------


def _pick(overall, player_id=None, espn_player_id=None, player_name=None):
    return SimpleNamespace(overall_pick=overall, player_id=player_id,
                           espn_player_id=espn_player_id, player_name=player_name)


@pytest.mark.unit
def test_a_player_already_in_the_draft_is_found_at_his_pick():
    existing = [_pick(1, player_id=203999, espn_player_id=3112335, player_name="Nikola Jokic"),
                _pick(2, player_id=1629029)]
    assert duplicate_pick_of(existing, 203999, 3112335, "Nikola Jokic") == 1
    assert duplicate_pick_of(existing, 1630162, None, "Somebody Else") is None


@pytest.mark.unit
def test_a_pick_recorded_before_the_player_synced_still_collides_with_him():
    """Pick 3 went in off an ESPN id and pick 4 off a name while neither player
    was in nba.players. The same players recorded again, resolved now, are dups."""
    existing = [_pick(3, espn_player_id=4433134), _pick(4, player_name="Cooper Flagg")]
    assert duplicate_pick_of(existing, 1642258, 4433134, "Victor Wembanyama") == 3
    assert duplicate_pick_of(existing, 1642259, None, "  cooper flagg ") == 4


@pytest.mark.unit
def test_two_resolved_players_who_share_a_name_are_not_the_same_pick():
    existing = [_pick(5, player_id=1628991, player_name="Jaren Jackson")]
    assert duplicate_pick_of(existing, 1630000, None, "Jaren Jackson") is None
    # ...but an unresolved pick with only that name is, until an id says otherwise.
    assert duplicate_pick_of([_pick(6, player_name="Jaren Jackson")], 1630000, None, "Jaren Jackson") == 6


@pytest.mark.unit
def test_a_racing_insert_is_named_by_the_index_it_tripped():
    player = IntegrityError('duplicate key value violates unique constraint "draft_picks_session_player_uq"')
    number = IntegrityError('duplicate key value violates unique constraint "draft_picks_session_overall_uq"')
    assert DraftService._pick_conflict(player, 7, "Star").error_code == "DRAFT_PLAYER_ALREADY_DRAFTED"
    assert DraftService._pick_conflict(number, 7, "Star").error_code == "DRAFT_PICK_ALREADY_EXISTS"


@pytest.mark.unit
def test_lagging_picks_resolve_by_espn_id_then_by_an_unambiguous_name(monkeypatch):
    from services import draft_service as module

    players = [
        SimpleNamespace(id=9, espn_id=109, name_normalized="rookie"),
        SimpleNamespace(id=21, espn_id=121, name_normalized="jaren jackson"),
        SimpleNamespace(id=22, espn_id=122, name_normalized="jaren jackson"),
    ]

    class _Query(list):
        def where(self, *_args):
            return self

    monkeypatch.setattr(module.Player, "select", classmethod(lambda cls, *fields: _Query(players)))
    picks = [
        _pick(1, espn_player_id=109, player_name="Somebody Else"),   # the ESPN id outranks the name
        _pick(2, player_name=" Rookie "),
        _pick(3, player_name="Jaren Jackson"),                      # two players: left alone
        _pick(4, player_id=5, espn_player_id=109),                  # already resolved: untouched
    ]
    assert resolve_lagging_picks(picks) == 2
    assert [p.player_id for p in picks] == [9, 9, None, 5]
    # Nothing to resolve, nothing queried.
    monkeypatch.setattr(module.Player, "select", classmethod(lambda cls, *f: pytest.fail("queried")))
    assert resolve_lagging_picks([_pick(4, player_id=5)]) == 0


# ---- the shape a session must keep ------------------------------------------


@pytest.mark.unit
def test_a_slot_must_be_a_seat_the_pick_order_has():
    check_slot_in_range(3, 10)
    check_slot_in_range(None, 10)
    check_slot_in_range(8, None)          # no pick order yet: nothing to be outside of
    with pytest.raises(BadRequestError) as exc:
        check_slot_in_range(8, 4)
    assert exc.value.error_code == "DRAFT_SLOT_OUT_OF_RANGE"


@pytest.mark.unit
def test_a_draft_cannot_be_resized_shorter_than_its_recorded_picks():
    check_length_holds_picks(52, [1, 2, 52])
    check_length_holds_picks(None, [60])  # no known length: nothing to be past
    check_length_holds_picks(52, [])
    with pytest.raises(BadRequestError) as exc:
        check_length_holds_picks(52, [1, 2, 60])
    assert exc.value.error_code == "DRAFT_SHORTER_THAN_PICKS"


@pytest.mark.unit
def test_a_picks_round_and_slot_follow_the_header():
    assert pick_geometry(11, 10, "snake") == (2, 10)
    assert pick_geometry(11, 10, "auction") == (None, None)
    assert pick_geometry(11, None, "snake") == (None, None)


# ---- keepers: picks spent before the draft starts -----------------------------


@pytest.mark.unit
def test_a_keeper_costs_its_slots_pick_in_that_round():
    # Slot 3 of a 10-team snake picks 3, 18, 23, ...
    assert pick_for_slot(1, 3, 10) == 3
    assert pick_for_slot(2, 3, 10) == 18
    assert pick_for_slot(3, 3, 10) == 23
    assert pick_for_slot(2, 3, 10, "auction") is None
    assert pick_for_slot(None, 3, 10) is None       # no round assigned yet
    assert pick_for_slot(2, None, 10) is None       # no slot confirmed yet
    assert pick_for_slot(2, 11, 10) is None         # no such seat


@pytest.mark.unit
def test_the_front_ignores_keeper_picks_and_steps_over_them():
    assert draft_front([]) == 1
    assert draft_front([1, 2, 3]) == 4
    assert draft_front([1, 2, 23], keeper_picks={23}) == 3     # a keeper at 23 has not moved the draft
    assert draft_front([1, 2, 3], keeper_picks={4}) == 5       # the front skips a spent pick
    assert draft_front([23], keeper_picks={23}) == 1


@pytest.mark.unit
def test_my_next_turn_skips_the_pick_my_keeper_spent():
    assert next_pick_for_slot(4, 3, 10, skip={18}) == 23


@pytest.mark.unit
def test_the_session_prices_its_keepers_and_counts_the_front_past_them():
    session = _session(keepers=[{"player_id": 9, "name": "Kept", "round": 2}])
    resp = _session_resp(session, used_picks=[1, 2, 3, 4, 18], keeper_count=1, keeper_picks=[18])

    assert resp.keepers[0].overall_pick == 18       # slot 3, round two
    assert resp.next_overall_pick == 5 and resp.pick_count == 5
    assert resp.my_next_pick == 23                  # 18 is spent; my next turn on the clock is 23
    assert resp.picks_until_my_turn == 17           # 23 - 5, less the spent 18 in between
    # A keeper without a round, or a session without a slot, has no pick to price.
    assert _session_resp(_session(keepers=[{"name": "Kept"}]), used_picks=[]).keepers[0].overall_pick is None
    assert _session_resp(_session(my_slot=None, keepers=[{"name": "K", "round": 2}]), used_picks=[]).keepers[0].overall_pick is None


# ---- a keeper pick has to be a keeper --------------------------------------


def _keeper(player_id=None, espn_player_id=None, name=None, round=None, overall_pick=None):
    return SimpleNamespace(player_id=player_id, espn_player_id=espn_player_id,
                           name=name, round=round, overall_pick=overall_pick)


def _kpick(overall, source="keeper", player_id=None, espn_player_id=None, player_name=None):
    return SimpleNamespace(overall_pick=overall, source=source, player_id=player_id,
                           espn_player_id=espn_player_id, player_name=player_name)


@pytest.mark.unit
def test_a_recorded_pick_is_matched_to_its_keeper_by_the_strongest_shared_id():
    keepers = [_keeper(player_id=9, name="Kept", overall_pick=18),
               _keeper(espn_player_id=555, name="Lagging", overall_pick=23)]
    assert matching_keeper(keepers, _kpick(18, player_id=9)).overall_pick == 18
    assert matching_keeper(keepers, _kpick(23, espn_player_id=555)).overall_pick == 23
    assert matching_keeper(keepers, _kpick(23, player_name=" lagging ")).overall_pick == 23
    assert matching_keeper(keepers, _kpick(4, player_id=11)) is None


@pytest.mark.unit
def test_repricing_a_keeper_moves_its_recorded_pick():
    """Slot 3 → slot 4 in a ten-team draft moves a round-two keeper from 18 to 17.
    The response reprices on read, so the row has to move with it."""
    moves = plan_keeper_moves(
        [_keeper(player_id=9, name="Kept", round=2, overall_pick=17)],
        [_kpick(18, player_id=9), _kpick(1, source="manual", player_id=1)],
    )
    assert moves == {18: 17}
    # Nothing to do when the number still matches.
    assert plan_keeper_moves([_keeper(player_id=9, overall_pick=18)], [_kpick(18, player_id=9)]) == {}
    # ...and no keeper picks at all is not a query.
    assert plan_keeper_moves([], [_kpick(3, source="manual")]) == {}


@pytest.mark.unit
def test_an_edit_that_strands_a_recorded_keeper_is_refused():
    # The keeper lost its round (or its designation, or the session its slot),
    # so the recorded pick has nowhere to sit.
    with pytest.raises(BadRequestError) as exc:
        plan_keeper_moves([_keeper(player_id=9, name="Kept", round=None)], [_kpick(18, player_id=9)])
    assert exc.value.error_code == "DRAFT_KEEPER_PICK_UNPRICED"

    with pytest.raises(BadRequestError) as exc:
        plan_keeper_moves([], [_kpick(18, player_id=9)])
    assert exc.value.error_code == "DRAFT_KEEPER_PICK_UNPRICED"


@pytest.mark.unit
def test_a_keeper_cannot_move_onto_a_pick_someone_already_holds():
    with pytest.raises(BadRequestError) as exc:
        plan_keeper_moves(
            [_keeper(player_id=9, overall_pick=17)],
            [_kpick(18, player_id=9), _kpick(17, source="manual", player_id=1)],
        )
    assert exc.value.error_code == "DRAFT_KEEPER_PICK_CONFLICT"


@pytest.mark.unit
def test_two_keepers_swapping_numbers_is_a_valid_plan():
    """Both targets are held only by picks that are themselves moving."""
    moves = plan_keeper_moves(
        [_keeper(player_id=9, overall_pick=23), _keeper(player_id=10, overall_pick=18)],
        [_kpick(18, player_id=9), _kpick(23, player_id=10)],
    )
    assert moves == {18: 23, 23: 18}


@pytest.mark.unit
def test_a_finished_draft_reports_no_next_turn_rather_than_a_round_it_never_plays():
    """Found by the replay: a 13-round, 4-team draft ends at 52, but the search
    ran on and answered 54 — seat 3's pick in a fourteenth round."""
    assert next_pick_for_slot(52, slot=3, league_size=4, last=52) is None
    assert next_pick_for_slot(52, slot=3, league_size=4) == 54       # unbounded, as before
    # A turn inside the draft is unaffected by the bound (seat 3 picks 3, 6,
    # 11, 14, ... 51 — the capture's own sequence).
    assert next_pick_for_slot(48, slot=3, league_size=4, last=52) == 51

    session = _session(pick_order=[2, 1, 4, 3], my_slot=3, rounds=13)
    done = _session_resp(session, used_picks=list(range(1, 53)), keeper_count=0)
    assert done.my_next_pick is None and done.picks_until_my_turn is None
    assert done.next_overall_pick == 53 and done.total_picks == 52
