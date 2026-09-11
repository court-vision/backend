"""
The fill-only planner and the IR housekeeping planner (services.lineup_planner):
pure, so every rule is a table of players in, moves out.

Slot ids are ESPN's: 0 PG, 1 SG, 2 SF, 3 PF, 4 C, 5 G, 6 F, 11 UT, 12 BE, 13 IR.
Slot counts come from the real ESPN settings fixture (1 of each position, 1 G,
1 F, 3 UT, 3 BE, 1 IR) so the tests speak the league's own vocabulary.
"""

import json
from pathlib import Path

import pytest

from services.lineup_planner import (
    BENCH_SLOT_ID,
    IR_SLOT_ID,
    Move,
    PlannerPlayer,
    apply_moves,
    plan_fill,
    plan_ir,
    validate_moves,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SETTINGS = json.loads((FIXTURES / "espn_settings_h2h_category.json").read_text())
SLOT_COUNTS = {int(k): v for k, v in SETTINGS["settings"]["rosterSettings"]["lineupSlotCounts"].items()}

PG, SG, SF, PF, C, G, F, UT, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13
GUARD = frozenset({PG, SG, G, UT, BE})
WING = frozenset({SF, PF, F, UT, BE})
BIG = frozenset({C, PF, F, UT, BE})


def player(pid, slot, eligible=GUARD, *, game=True, status=None, locked=False, value=10.0, name=None):
    return PlannerPlayer(
        player_id=pid, name=name or f"P{pid}", slot_id=slot, eligible_slot_ids=frozenset(eligible),
        has_game=game, injury_status=status, locked=locked, value=value,
        game_note="vs LAL · 7:30 PM" if game else None,
    )


def full_lineup(overrides=None):
    """A legal 13-man roster: one starter per slot, three UT, three bench, nobody on IR."""
    base = {
        1: player(1, PG), 2: player(2, SG), 3: player(3, SF, WING), 4: player(4, PF, WING), 5: player(5, C, BIG),
        6: player(6, G), 7: player(7, F, WING), 8: player(8, UT), 9: player(9, UT, WING), 10: player(10, UT, BIG),
        11: player(11, BE), 12: player(12, BE, WING), 13: player(13, BE, BIG),
    }
    base.update(overrides or {})
    return list(base.values())


def moves_of(plan):
    return [(m.player_id, m.from_slot_id, m.to_slot_id, m.role) for m in plan.moves]


# ---- plan_fill ---------------------------------------------------------------------


@pytest.mark.unit
def test_everyone_plays_and_healthy_is_a_noop():
    plan = plan_fill(full_lineup(), SLOT_COUNTS)
    assert plan.is_noop and plan.unfilled == ()
    assert "Nothing to fill" in plan.summary


@pytest.mark.unit
def test_no_game_starter_swaps_with_a_playing_bench_player():
    roster = full_lineup({8: player(8, UT, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(11, BE, UT, "start"), (8, UT, BE, "bench")]
    assert plan.moves[0].note == "vs LAL · 7:30 PM" and plan.moves[1].note == "no game today"
    assert plan.unfilled == ()


@pytest.mark.unit
def test_out_starter_is_kept_when_no_healthy_bench_player_needs_the_slot():
    """Tier B (OUT, has a game) is never evicted for nothing."""
    roster = full_lineup({5: player(5, C, BIG, status="OUT"), 11: player(11, BE, game=False),
                          12: player(12, BE, WING, game=False), 13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert plan.is_noop


@pytest.mark.unit
def test_out_starter_yields_to_a_healthy_bench_player():
    roster = full_lineup({5: player(5, C, BIG, status="OUT"), 11: player(11, BE, game=False),
                          12: player(12, BE, WING, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(13, BE, C, "start"), (5, C, BE, "bench")]
    assert plan.moves[1].note == "OUT"


@pytest.mark.unit
def test_healthy_guard_reaches_an_out_center_through_a_chain():
    """The bench guard cannot play C, but the UT big can shift there — so the OUT
    center still sits: start guard at UT, shift big UT->C, bench the center."""
    roster = full_lineup({5: player(5, C, BIG, status="OUT"), 12: player(12, BE, WING, game=False),
                          13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(11, BE, UT, "start"), (10, UT, C, "shift"), (5, C, BE, "bench")]


@pytest.mark.unit
def test_out_bench_player_fills_an_empty_slot_but_never_displaces_a_starter():
    """Tier B on the bench: worth starting into an open slot, not worth a swap."""
    idle = {11: player(11, BE, game=False), 12: player(12, BE, WING, game=False)}
    roster = full_lineup({**idle, 13: player(13, BE, BIG, status="OUT")})
    roster = [p for p in roster if p.player_id != 10]  # third UT empty
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(13, BE, UT, "start")]
    roster = full_lineup({**idle, 13: player(13, BE, BIG, status="OUT"), 5: player(5, C, BIG, status="OUT")})
    assert plan_fill(roster, SLOT_COUNTS).is_noop


@pytest.mark.unit
def test_out_bench_player_does_displace_a_no_game_starter():
    roster = full_lineup({5: player(5, C, BIG, game=False), 11: player(11, BE, game=False),
                          12: player(12, BE, WING, game=False), 13: player(13, BE, BIG, status="OUT")})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(13, BE, C, "start"), (5, C, BE, "bench")]


@pytest.mark.unit
def test_healthy_starter_is_never_benched():
    roster = full_lineup({11: player(11, BE, value=99.0)})  # star on the bench, everyone else healthy
    assert plan_fill(roster, SLOT_COUNTS).is_noop


@pytest.mark.unit
def test_chain_shifts_a_starter_to_open_the_only_eligible_slot():
    """Bench C-only player; the hole is at UT held by a guard who cannot take C — but
    the C starter can shift to UT, so the chain is start C, shift C->UT, bench UT."""
    roster = full_lineup({
        8: player(8, UT, game=False),
        13: player(13, BE, frozenset({C, BE})),
        5: player(5, C, frozenset({C, UT, BE})),
        11: player(11, BE, game=False), 12: player(12, BE, game=False),
    })
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(13, BE, C, "start"), (5, C, UT, "shift"), (8, UT, BE, "bench")]


@pytest.mark.unit
def test_two_fillers_one_hole_picks_the_higher_value():
    roster = full_lineup({8: player(8, UT, game=False), 11: player(11, BE, value=5.0), 12: player(12, BE, WING, value=30.0),
                            13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(12, BE, UT, "start"), (8, UT, BE, "bench")]
    assert plan.unfilled == ()  # once the hole is filled, nobody worse is starting


@pytest.mark.unit
def test_two_holes_prefers_the_worse_holder_first():
    """A no-game holder (tier C) sits before an OUT holder (tier B) when one filler is available."""
    roster = full_lineup({8: player(8, UT, status="OUT"), 9: player(9, UT, WING, game=False),
                            12: player(12, BE, WING, game=False), 13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(11, BE, UT, "start"), (9, UT, BE, "bench")]


@pytest.mark.unit
def test_locked_holder_blocks_and_is_reported():
    roster = full_lineup({8: player(8, UT, game=False, locked=True), 9: player(9, UT, WING, locked=True),
                            10: player(10, UT, BIG, locked=True), 12: player(12, BE, WING, game=False),
                            13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert plan.is_noop
    assert [(u.player_id, u.reason) for u in plan.unfilled] == [(11, "slot_holder_locked")]


@pytest.mark.unit
def test_locked_filler_is_skipped_silently():
    roster = full_lineup({8: player(8, UT, game=False), 11: player(11, BE, locked=True),
                            12: player(12, BE, WING, game=False), 13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert plan.is_noop and plan.unfilled == ()


@pytest.mark.unit
def test_ir_and_unused_slots_are_never_touched():
    roster = full_lineup({8: player(8, UT, game=False), 11: player(11, BE, game=False),
                            12: player(12, BE, WING, game=False), 13: player(13, BE, BIG, game=False)})
    roster.append(player(14, IR_SLOT_ID, frozenset({PG, IR, BE}), value=50.0))  # healthy on IR
    roster.append(player(15, 14, frozenset({PG, BE}), value=50.0))               # slot 14 oddity
    assert plan_fill(roster, SLOT_COUNTS).is_noop


@pytest.mark.unit
def test_empty_active_slot_is_filled_with_a_single_move():
    roster = [p for p in full_lineup() if p.player_id != 6]  # G empty
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(11, BE, G, "start")]


@pytest.mark.unit
def test_dtd_bench_player_is_a_filler_and_out_is_lower_priority():
    roster = full_lineup({8: player(8, UT, game=False), 11: player(11, BE, status="DAY_TO_DAY", value=1.0),
                            12: player(12, BE, WING, status="OUT", value=50.0), 13: player(13, BE, BIG, game=False)})
    plan = plan_fill(roster, SLOT_COUNTS)
    assert moves_of(plan) == [(11, BE, UT, "start"), (8, UT, BE, "bench")]


@pytest.mark.unit
def test_no_game_bench_player_never_moves_even_into_an_empty_slot():
    roster = [p for p in full_lineup({11: player(11, BE, game=False), 12: player(12, BE, WING, game=False),
                                        13: player(13, BE, BIG, game=False)}) if p.player_id != 6]
    assert plan_fill(roster, SLOT_COUNTS).is_noop


# ---- validate_moves / apply_moves ---------------------------------------------------------


def codes(errors):
    return [e.code for e in errors]


@pytest.mark.unit
def test_valid_swap_passes_and_apply_moves_reassigns():
    roster = full_lineup()
    moves = [Move(11, BE, UT), Move(8, UT, BE)]
    assert validate_moves(roster, SLOT_COUNTS, moves) == []
    after = {p.player_id: p.slot_id for p in apply_moves(roster, moves)}
    assert after[11] == UT and after[8] == BE


@pytest.mark.unit
@pytest.mark.parametrize("moves, expected", [
    ([Move(99, BE, UT)], ["UNKNOWN_PLAYER"]),
    ([Move(11, BE, UT), Move(11, BE, G)], ["DUPLICATE_PLAYER"]),
    ([Move(11, BE, BE)], ["SAME_SLOT"]),
    ([Move(11, 14, UT)], ["UNTOUCHABLE_SLOT"]),
    ([Move(11, UT, BE)], ["STALE_SLOT"]),
    ([Move(11, BE, SF)], ["INELIGIBLE"]),
    ([Move(11, BE, IR)], ["INELIGIBLE"]),                 # 13 not in eligible_slot_ids => ESPN would refuse
    ([Move(11, BE, UT)], ["CAPACITY"]),                    # 4 in UT
    ([Move(1, PG, BE)], ["CAPACITY"]),                     # 4 on the bench
])
def test_each_validation_code(moves, expected):
    roster = full_lineup()
    assert codes(validate_moves(roster, SLOT_COUNTS, moves)) == expected


@pytest.mark.unit
def test_locked_player_cannot_be_moved():
    roster = full_lineup({8: player(8, UT, locked=True)})
    assert codes(validate_moves(roster, SLOT_COUNTS, [Move(8, UT, BE), Move(11, BE, UT)])) == ["LOCKED"]


@pytest.mark.unit
def test_ir_needs_an_injured_player_not_just_the_slot_in_the_list():
    """ESPN lists 13 for everyone and refuses healthy players at transaction time
    (TRAN_ROSTER_INELIGIBLE_IR_NOT_INJURED, captured 2026-09-08) — so we refuse first."""
    everyone_ir = frozenset({PG, UT, IR, BE})
    healthy = full_lineup({8: player(8, UT, everyone_ir)})
    errors = validate_moves(healthy, SLOT_COUNTS, [Move(8, UT, IR), Move(11, BE, UT)])
    assert codes(errors) == ["INELIGIBLE"] and "IR" in errors[0].message and "injured" in errors[0].message
    out = full_lineup({8: player(8, UT, everyone_ir, status="OUT")})
    assert validate_moves(out, SLOT_COUNTS, [Move(8, UT, IR), Move(11, BE, UT)]) == []
    flagged = full_lineup({8: PlannerPlayer(8, "P8", UT, everyone_ir, True, "DAY_TO_DAY", False, 10.0, injured=True)})
    assert validate_moves(flagged, SLOT_COUNTS, [Move(8, UT, IR), Move(11, BE, UT)]) == []


@pytest.mark.unit
def test_a_suspension_is_not_an_injury_so_it_does_not_open_ir():
    """SUSPENSION sits in OUT_STATUSES for tiering, but ESPN still refuses the IR move."""
    everyone_ir = frozenset({PG, UT, IR, BE})
    suspended = player(8, UT, everyone_ir, status="SUSPENSION")
    assert suspended.is_out is True and suspended.ir_eligible is False   # unavailable, not injured
    errors = validate_moves(full_lineup({8: suspended}), SLOT_COUNTS, [Move(8, UT, IR), Move(11, BE, UT)])
    assert codes(errors) == ["INELIGIBLE"] and "injured" in errors[0].message

    # ESPN's own flag still wins: a suspended player it also marks injured may go on IR.
    also_hurt = PlannerPlayer(8, "P8", UT, everyone_ir, True, "SUSPENSION", False, 10.0, injured=True)
    assert also_hurt.ir_eligible is True
    assert validate_moves(full_lineup({8: also_hurt}), SLOT_COUNTS, [Move(8, UT, IR), Move(11, BE, UT)]) == []


@pytest.mark.unit
def test_capacity_holds_after_a_three_move_chain():
    roster = full_lineup({5: player(5, C, frozenset({C, UT, BE})), 13: player(13, BE, frozenset({C, BE}))})
    chain = [Move(13, BE, C), Move(5, C, UT), Move(8, UT, BE)]
    assert validate_moves(roster, SLOT_COUNTS, chain) == []
    broken = [Move(13, BE, C), Move(5, C, UT)]  # nobody leaves UT: 4 in UT
    assert codes(validate_moves(roster, SLOT_COUNTS, broken)) == ["CAPACITY"]


# ---- plan_ir ---------------------------------------------------------------------

# ESPN lists slot 13 for everyone; the fixture sets above leave it out so the
# eligibility rule is explicit in every test that needs it.
IR_GUARD = GUARD | {IR}
IR_BIG = BIG | {IR}


def ir_of(actions):
    return [(a.kind, a.player_id, (a.move.from_slot_id, a.move.to_slot_id) if a.move else None, a.blocked_reason)
            for a in actions]


def without(roster, pid):
    return [p for p in roster if p.player_id != pid]


@pytest.mark.unit
def test_healthy_full_board_has_no_ir_actions():
    assert plan_ir(full_lineup(), SLOT_COUNTS) == ()


@pytest.mark.unit
def test_out_starter_takes_the_open_ir_seat():
    actions = plan_ir(full_lineup({5: player(5, C, IR_BIG, status="OUT")}), SLOT_COUNTS)
    assert ir_of(actions) == [("ir_in", 5, (C, IR), None)]
    assert actions[0].move.role == "shift" and actions[0].move.note == "OUT"


@pytest.mark.unit
def test_one_seat_goes_to_a_starter_before_a_bench_player_then_to_the_lowest_value():
    starter_and_bench = full_lineup({11: player(11, BE, IR_GUARD, status="OUT", value=5.0),
                                     5: player(5, C, IR_BIG, status="OUT", value=30.0)})
    assert ir_of(plan_ir(starter_and_bench, SLOT_COUNTS)) == [("ir_in", 5, (C, IR), None)]
    two_starters = full_lineup({1: player(1, PG, IR_GUARD, status="OUT", value=30.0),
                                5: player(5, C, IR_BIG, status="OUT", value=12.0)})
    assert ir_of(plan_ir(two_starters, SLOT_COUNTS)) == [("ir_in", 5, (C, IR), None)]


@pytest.mark.unit
def test_a_full_ir_offers_nothing():
    roster = full_lineup({5: player(5, C, IR_BIG, status="OUT")}) + [player(14, IR, IR_GUARD, status="OUT")]
    assert plan_ir(roster, SLOT_COUNTS) == ()


@pytest.mark.unit
def test_healthy_ir_player_returns_to_a_bench_seat():
    roster = without(full_lineup(), 13) + [player(14, IR, IR_GUARD)]
    actions = plan_ir(roster, SLOT_COUNTS)
    assert ir_of(actions) == [("ir_out", 14, (IR, BE), None)]
    assert actions[0].move.role == "shift" and actions[0].move.note == "healthy"


@pytest.mark.unit
def test_healthy_ir_player_with_a_full_roster_is_blocked_not_moved():
    roster = full_lineup() + [player(14, IR, IR_GUARD)]
    assert ir_of(plan_ir(roster, SLOT_COUNTS)) == [("ir_out", 14, None, "roster_full")]


@pytest.mark.unit
def test_healthy_ir_player_takes_an_open_active_slot_when_the_bench_is_full():
    roster = without(full_lineup(), 1) + [player(14, IR, IR_GUARD)]   # PG empty, bench full
    assert ir_of(plan_ir(roster, SLOT_COUNTS)) == [("ir_out", 14, (IR, PG), None)]


@pytest.mark.unit
def test_locked_players_never_move_on_or_off_ir():
    roster = without(full_lineup({5: player(5, C, IR_BIG, status="OUT", locked=True)}), 13)
    roster += [player(14, IR, IR_GUARD, locked=True)]
    assert plan_ir(roster, SLOT_COUNTS) == ()


@pytest.mark.unit
def test_ir_eligibility_is_espns_injury_rule_not_the_status_alone():
    assert plan_ir(full_lineup({5: player(5, C, IR_BIG, status="SUSPENSION")}), SLOT_COUNTS) == ()
    flagged = full_lineup({5: PlannerPlayer(5, "P5", C, IR_BIG, True, "DAY_TO_DAY", False, 10.0, injured=True)})
    assert ir_of(plan_ir(flagged, SLOT_COUNTS)) == [("ir_in", 5, (C, IR), None)]
    assert plan_ir(full_lineup({5: player(5, C, BIG, status="OUT")}), SLOT_COUNTS) == ()   # 13 not listed


@pytest.mark.unit
def test_an_ir_out_does_not_free_the_seat_for_an_ir_in_in_the_same_pass():
    roster = without(full_lineup({5: player(5, C, IR_BIG, status="OUT")}), 13) + [player(14, IR, IR_GUARD)]
    assert ir_of(plan_ir(roster, SLOT_COUNTS)) == [("ir_out", 14, (IR, BE), None)]
