"""
The mock autopicker's simulation: who each seat takes, and where the run stops.

Everything here drives the pure layer — `simulate` decides and never writes, and
the cap rule is injected — so none of it needs a database. That matters: CI runs
the integration suite only off pull requests, and these are the assertions that
have to hold on every one of them.

The candidate pool is fabricated (`_pool(n)`: player `i` at order key `i`), so
"the best available" is readable straight off the id.
"""

import pytest

from services.draft_mock_service import (
    REACH_DEPTH,
    MockCandidate,
    MockGeometry,
    mock_rng,
    order_key_of,
    reach_index,
    simulate,
    sort_candidates,
)

pytestmark = pytest.mark.unit


def _pool(count: int, start: int = 1) -> list[MockCandidate]:
    """`count` candidates, best first: player i is the i-th name on the board."""
    return [
        MockCandidate(player_id=i, espn_id=1000 + i, name=f"Player {i}", order_key=float(i))
        for i in range(start, start + count)
    ]


def _geometry(**overrides) -> MockGeometry:
    """The replay league's shape: a 4-team, 13-round snake, me in seat 3."""
    base = dict(
        session_id=7, league_size=4, draft_type="snake", total_picks=52, front=1, my_slot=3,
    )
    base.update(overrides)
    return MockGeometry(**base)


def _no_caps(_roster):
    return lambda pid: False


def _run(geometry=None, candidates=None, seat_rosters=None, cap_check_for=_no_caps, until="my_turn"):
    return simulate(
        geometry or _geometry(),
        candidates if candidates is not None else _pool(60),
        seat_rosters or {},
        cap_check_for,
        until=until,
    )


# ---- determinism -----------------------------------------------------------


def test_the_same_room_and_pick_always_seed_the_same_rng():
    """The promise the whole feature rests on: a mock replays."""
    assert [mock_rng(7, 12).random() for _ in range(3)] == [mock_rng(7, 12).random() for _ in range(3)]


def test_a_different_pick_or_a_different_room_seeds_differently():
    """Per-pick seeding, and per-room: two rooms drafting identically would make
    'run another mock' pointless."""
    assert mock_rng(7, 12).random() != mock_rng(7, 13).random()
    assert mock_rng(7, 12).random() != mock_rng(8, 12).random()


def test_advancing_pick_by_pick_produces_the_same_draft_as_advancing_in_one_call():
    """The property per-pick seeding exists for. A per-advance RNG passes every
    other test in this file and fails this one."""
    one_shot = _run(until="end")
    assert len(one_shot.picks) == 52

    stitched = []
    taken = set()
    for step in range(52):
        result = _run(
            geometry=_geometry(front=step + 1, used=frozenset(range(1, step + 1))),
            candidates=[c for c in _pool(60) if c.player_id not in taken],
            until="end",
        )
        pick = result.picks[0]
        stitched.append((pick.overall_pick, pick.candidate.player_id))
        taken.add(pick.candidate.player_id)

    assert stitched == [(p.overall_pick, p.candidate.player_id) for p in one_shot.picks]


# ---- reaching --------------------------------------------------------------


def test_a_seat_never_reaches_past_the_window():
    rng = mock_rng(1, 1)
    assert all(reach_index(rng, 40) < REACH_DEPTH for _ in range(500))


def test_the_only_candidate_is_always_taken():
    rng = mock_rng(1, 1)
    assert all(reach_index(rng, 1) == 0 for _ in range(50))


def test_the_weights_renormalize_when_the_board_runs_short():
    """Two names left is still a choice — 0.6 against 1.0 — not a forced take."""
    rng = mock_rng(4, 4)
    draws = [reach_index(rng, 2) for _ in range(20_000)]
    assert set(draws) == {0, 1}
    assert draws.count(0) / len(draws) == pytest.approx(1 / 1.6, abs=0.02)


def test_the_best_available_is_the_usual_pick_and_the_fifth_name_is_rare():
    draws = [reach_index(mock_rng(1, n), REACH_DEPTH) for n in range(20_000)]
    assert draws.count(0) / len(draws) == pytest.approx(1 / 2.3056, abs=0.02)
    assert draws.count(4) / len(draws) == pytest.approx(0.1296 / 2.3056, abs=0.01)


# ---- ordering --------------------------------------------------------------


def test_adp_is_preferred_over_the_editorial_rank():
    """What real drafts did beats what ESPN says they should have done."""
    assert order_key_of(14.6, 9) == 14.6
    assert order_key_of(None, 9) == 9.0
    assert order_key_of(None, None) is None


def test_equal_keys_break_on_player_id_rather_than_row_order():
    """Otherwise a mock replays differently after Postgres returns the rows in
    another order, which is the promise this module makes."""
    tied = [
        MockCandidate(player_id=9, order_key=3.0),
        MockCandidate(player_id=2, order_key=3.0),
        MockCandidate(player_id=5, order_key=1.0),
    ]
    assert [c.player_id for c in sort_candidates(tied)] == [5, 2, 9]
    assert sort_candidates(tied) == sort_candidates(list(reversed(tied)))


# ---- where the run stops ---------------------------------------------------


def test_my_turn_stops_on_the_pick_before_mine():
    result = _run(until="my_turn")
    assert [p.overall_pick for p in result.picks] == [1, 2]
    assert result.stopped_at == 3 and result.stopped_reason == "my_turn"
    assert not any(p.by_me for p in result.picks)


def test_my_turn_makes_no_picks_when_i_am_already_on_the_clock():
    """The button is idempotent: pressing it twice is not two rounds."""
    result = _run(geometry=_geometry(front=3, used=frozenset({1, 2})), until="my_turn")
    assert result.picks == () and result.stopped_at == 3
    assert result.stopped_reason == "my_turn"


def test_my_turn_runs_to_the_end_once_my_last_turn_is_behind_me():
    """How a mock finishes on 'Sim to my pick' alone — my seat's last turn is
    pick 51 of 52, so the button has to be able to draft the 52nd."""
    result = _run(geometry=_geometry(front=52, used=frozenset(range(1, 52))), until="my_turn")
    assert [p.overall_pick for p in result.picks] == [52]
    assert result.stopped_reason == "end" and result.stopped_at is None


def test_end_fills_the_whole_draft_including_my_own_seat():
    result = _run(until="end")
    assert [p.overall_pick for p in result.picks] == list(range(1, 53))
    assert result.stopped_reason == "end" and result.stopped_at is None
    # Seat 3 of a 4-team snake: 3, 6, 11, 14, ... — the picks marked mine are
    # exactly the seat's, and nobody else's.
    assert {p.overall_pick for p in result.picks if p.by_me} == {
        3, 6, 11, 14, 19, 22, 27, 30, 35, 38, 43, 46, 51
    }
    assert all(p.slot == 3 for p in result.picks if p.by_me)


def test_nothing_is_mine_when_the_room_has_no_slot():
    """A room watching a draft happen. `end` is allowed without a slot; the
    picks belong to nobody."""
    result = _run(geometry=_geometry(my_slot=None), until="end")
    assert len(result.picks) == 52 and not any(p.by_me for p in result.picks)


def test_one_player_one_pick():
    picked = [p.candidate.player_id for p in _run(until="end").picks]
    assert len(set(picked)) == len(picked) == 52


def test_keeper_numbers_are_stepped_over_rather_than_filled():
    """A keeper is a pick spent before the draft started: the autopicker neither
    fills it again nor counts it as a turn."""
    result = _run(geometry=_geometry(used=frozenset({4, 5}), keeper_picks=frozenset({4, 5})), until="end")
    made = [p.overall_pick for p in result.picks]
    assert 4 not in made and 5 not in made
    assert len(made) == 50 and made[0] == 1


def test_an_empty_board_stops_the_run_at_once():
    result = _run(candidates=[], until="end")
    assert result.picks == () and result.stopped_reason == "pool_exhausted"
    assert result.stopped_at == 1 and result.blocked_slot is None


def test_the_run_stops_when_the_board_is_drafted_out():
    result = _run(candidates=_pool(10), until="end")
    assert len(result.picks) == 10
    assert result.stopped_reason == "pool_exhausted" and result.stopped_at == 11


# ---- hard caps -------------------------------------------------------------


def _cap_after(limit: int):
    """A toy cap rule: no seat may hold more than `limit` players."""
    def factory(roster):
        return lambda pid: len(roster) >= limit
    return factory


def test_a_cap_stops_the_run_and_is_never_breached():
    """Stopping is the honest answer. Relaxing the cap silently would make the
    board's own CAP badges a lie."""
    result = _run(cap_check_for=_cap_after(2), until="end")
    per_seat: dict[int, int] = {}
    for pick in result.picks:
        per_seat[pick.slot] = per_seat.get(pick.slot, 0) + 1
    assert max(per_seat.values()) == 2
    assert result.stopped_reason == "cap_blocked"
    assert result.blocked_slot is not None and result.stopped_at == len(result.picks) + 1


def test_a_seat_already_holding_its_limit_blocks_immediately():
    result = _run(
        seat_rosters={1: frozenset({901, 902})},
        cap_check_for=_cap_after(2),
        until="end",
    )
    assert result.picks == () and result.stopped_reason == "cap_blocked"
    assert result.blocked_slot == 1


def test_a_player_one_seat_cannot_take_is_still_there_for_the_next():
    """Caps are counted per seat, so a cap is not a removal from the board.

    Two players, two picks: seat 1 is the only one capped out of the best
    available, so it takes the second name — and the first is still there for
    seat 2. A cap that removed him from the queue would leave seat 2 with
    nothing.
    """
    def seat_one_cannot_take_the_best(roster):
        # The marker player stands in for whatever puts seat 1 over its cap;
        # the factory sees a roster, not a seat number.
        return lambda pid: 999 in roster and pid == 1

    result = _run(
        geometry=_geometry(total_picks=2),
        candidates=_pool(2),
        seat_rosters={1: frozenset({999})},
        cap_check_for=seat_one_cannot_take_the_best,
        until="end",
    )

    assert [(p.slot, p.candidate.player_id) for p in result.picks] == [(1, 2), (2, 1)]
    assert result.stopped_reason == "end"
