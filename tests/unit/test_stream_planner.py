"""
services.stream_planner: the stream lane's optimizer.

The behaviours the plan must show without any rule written for them: replacing a
holder who plays when there are adds to spare, waiting for his day off when there
aren't. The constraints: the budget, one switch a day and only on switchable days,
waivers (a free agent's own, and the gap before a player we drop can come back, the
holder included). Ties keep the holder. Optimality is checked against brute force
on random small horizons.
"""

import itertools
import random
import time

import pytest

from services.stream_planner import (
    Candidate,
    DayPlayer,
    best_lineup_value,
    plan_stream,
    spot_values,
)

PG, SG, G, UT = 0, 1, 5, 11
HOLDER = 1


def c(pid, *values, available_from=0):
    return Candidate(pid, tuple(float(v) for v in values), available_from)


# ---- one day's lineup --------------------------------------------------------------------


@pytest.mark.unit
def test_best_lineup_moves_a_flexible_player_to_seat_one_who_isnt():
    players = [DayPlayer(1, 50.0, frozenset({PG, UT})), DayPlayer(2, 40.0, frozenset({UT}))]
    assert best_lineup_value(players, {PG: 1, UT: 1}) == 90.0


@pytest.mark.unit
def test_best_lineup_leaves_the_lowest_out_and_ignores_the_bench_and_ir():
    players = [DayPlayer(i, v, frozenset({UT})) for i, v in ((1, 30), (2, 20), (3, 25))]
    assert best_lineup_value(players, {UT: 2, 12: 3, 13: 1}) == 55.0


@pytest.mark.unit
def test_spot_values_are_what_he_adds_to_each_days_lineup():
    slots = {PG: 1, UT: 1}
    rest = [DayPlayer(10, 30.0, frozenset({PG, UT})), DayPlayer(11, 20.0, frozenset({PG}))]
    rest_by_day = [rest, rest, rest]
    # A UT-only player beats the PG-only one by moving the flexible player to PG: +5.
    ut = [DayPlayer(1, 25.0, frozenset({UT})), None, DayPlayer(1, 0.0, frozenset({UT}))]
    assert spot_values(rest_by_day, slots, ut) == (5.0, 0.0, 0.0)
    # A PG-only player who can't beat the PG already there adds nothing: he wouldn't start.
    pg = [DayPlayer(2, 15.0, frozenset({PG}))] * 3
    assert spot_values(rest_by_day, slots, pg) == (0.0, 0.0, 0.0)
    # Into an open seat, his whole value.
    assert spot_values([[], [], []], slots, pg) == (15.0, 15.0, 15.0)


# ---- the plan: what emerges -----------------------------------------------------------------


@pytest.mark.unit
def test_with_adds_to_spare_it_replaces_a_holder_who_plays():
    holder = c(HOLDER, 20, 20, 20, 20)
    plan = plan_stream(holder, [c(2, 30, 30, 0, 0), c(3, 0, 0, 30, 30)], adds=4)
    assert plan.holders == (2, 2, 3, 3)
    assert (plan.value, plan.hold_value, plan.gain, plan.adds) == (120.0, 80.0, 40.0, 2)
    assert plan.first_move.player_id == 2 and plan.first_move.start == 0


@pytest.mark.unit
def test_with_one_add_it_waits_for_the_holders_day_off():
    """The holder plays days 0, 1, 3, 4; the free agent days 2, 5, 6. One add: switching on day 2
    costs the holder's 3 and 4 for one more game, so the plan waits until day 5."""
    holder = c(HOLDER, 10, 10, 0, 10, 10, 0, 0)
    x = c(2, 0, 0, 10, 0, 0, 10, 10)
    plan = plan_stream(holder, [x], adds=1)
    assert plan.holders == (1, 1, 1, 1, 1, 2, 2)
    assert (plan.value, plan.adds, plan.first_move) == (60.0, 1, None)
    # A one-day player for day 2 would need the holder back on day 3, inside his waiver gap.
    plan = plan_stream(holder, [x, c(3, 0, 0, 10, 0, 0, 0, 0)], adds=7)
    assert plan.value == 60.0 and 3 not in plan.holders


@pytest.mark.unit
def test_a_dropped_player_waits_out_his_waivers_before_coming_back():
    """A plays days 0, 1, 3, 4 and B day 2. A, B, A would take A back the day after dropping him."""
    holder = c(HOLDER, 0, 0, 0, 0, 0)
    a, b = c(2, 20, 20, 0, 20, 20), c(3, 0, 0, 15, 0, 0)
    plan = plan_stream(holder, [a, b], adds=None)            # two days of waivers
    assert plan.holders == (2, 2, 2, 2, 2) and plan.value == 80.0 and plan.exact
    # One day of waivers: back on day 3 is fine.
    plan = plan_stream(holder, [a, b], adds=None, rehold_after=1)
    assert plan.holders == (2, 2, 3, 2, 2) and plan.value == 95.0 and plan.adds == 3
    # With someone for day 3 too, filling day 2 pays: A's waivers clear by day 4.
    plan = plan_stream(holder, [a, b, c(4, 0, 0, 0, 18, 0)], adds=None)
    assert plan.holders == (2, 2, 3, 4, 2) and plan.value == 93.0


@pytest.mark.unit
def test_the_holder_comes_back_only_once_his_waivers_clear():
    """Dropping the holder for days 1 and 2 pays only if he's back for his big day 3."""
    holder = c(HOLDER, 10, 0, 0, 50)
    a = c(2, 0, 20, 20, 0)
    plan = plan_stream(holder, [a], adds=None)                # dropped for day 1, back from day 3
    assert plan.holders == (1, 2, 2, 1) and plan.value == 100.0 and plan.adds == 2
    # Three days of waivers: drop him a day earlier, giving up his 10, so he's back for the 50.
    sooner = plan_stream(holder, [a], adds=None, rehold_after=3)
    assert sooner.holders == (2, 2, 2, 1) and sooner.value == 90.0
    never = plan_stream(holder, [a], adds=None, rehold_after=None)
    assert never.holders == (1, 1, 1, 1) and never.value == 60.0 and never.adds == 0
    # Two adds are needed: with one, he stays.
    assert plan_stream(holder, [a], adds=1).holders == (1, 1, 1, 1)


# ---- the plan: constraints ------------------------------------------------------------------


@pytest.mark.unit
def test_no_adds_or_no_candidates_keep_the_holder():
    holder = c(HOLDER, 5, 5)
    for plan in (plan_stream(holder, [c(2, 30, 30)], adds=0), plan_stream(holder, [], adds=None)):
        assert plan.holders == (1, 1) and plan.gain == 0.0 and plan.first_move is None and plan.adds == 0


@pytest.mark.unit
def test_one_switch_a_day_and_only_on_switchable_days():
    holder = c(HOLDER, 0, 0, 0)
    a = c(2, 30, 0, 0)
    b = c(3, 25, 25, 25)
    # Day 0's allowance is spent: nobody can take the spot until day 1.
    plan = plan_stream(holder, [a, b], adds=None, switchable=[False, True, True])
    assert plan.holders == (1, 3, 3) and plan.value == 50.0


@pytest.mark.unit
def test_a_player_on_waivers_waits_until_he_clears():
    holder = c(HOLDER, 0, 0, 0)
    plan = plan_stream(holder, [c(2, 40, 40, 40, available_from=2)], adds=None)
    assert plan.holders == (1, 1, 2) and plan.value == 40.0


@pytest.mark.unit
def test_ties_keep_the_holder_and_switch_as_late_as_it_costs_nothing():
    assert plan_stream(c(HOLDER, 10), [c(2, 10)], adds=1).first_move is None
    # Switching on day 0 or day 1 is the same 10 (the free agent first plays day 1): wait.
    plan = plan_stream(c(HOLDER, 0, 0), [c(2, 0, 10)], adds=1)
    assert plan.holders == (1, 2) and plan.first_move is None


@pytest.mark.unit
def test_the_horizon_must_match():
    with pytest.raises(ValueError):
        plan_stream(c(HOLDER, 1, 1), [c(2, 1)], adds=None)
    with pytest.raises(ValueError):
        plan_stream(c(HOLDER, 1, 1), [], adds=None, switchable=[True])


# ---- optimality ----------------------------------------------------------------------------


def brute_force(holder, candidates, adds, switchable, rehold_after):
    """Every legal day-by-day holder sequence, scored."""
    horizon = len(holder.values)
    budget = horizon if adds is None else min(adds, horizon)
    gap = horizon + 1 if rehold_after is None else rehold_after
    others = [x for x in candidates if x.player_id != holder.player_id]
    by_id = {x.player_id: x for x in [holder, *others]}
    best = 0.0
    for seq in itertools.product([holder.player_id, *(x.player_id for x in others)], repeat=horizon):
        prev, clears, spent, ok = holder.player_id, {}, 0, True
        for d, pid in enumerate(seq):
            if pid == prev:
                continue
            if clears.get(pid, -1) > d or not switchable[d] or d < by_id[pid].available_from:
                ok = False
                break
            clears[prev] = d + gap
            spent += 1
            prev = pid
        if ok and spent <= budget:
            best = max(best, sum(by_id[pid].values[d] for d, pid in enumerate(seq)))
    return best


@pytest.mark.unit
def test_matches_brute_force_on_random_small_horizons():
    rng = random.Random(20261011)
    for _ in range(300):
        horizon = rng.randint(1, 5)
        n = rng.randint(0, 3)

        def values():
            # Mostly zeros (no game), so gaps, re-add temptations and ties all come up.
            return tuple(float(rng.choice([0, 0, 0, 10, 15, 20, 25])) for _ in range(horizon))

        holder = Candidate(HOLDER, values())
        candidates = [Candidate(10 + k, values(), rng.choice([0, 0, 0, 1, 2])) for k in range(n)]
        adds = rng.choice([None, 0, 1, 2, 3])
        switchable = [rng.random() > 0.2 for _ in range(horizon)]
        rehold_after = rng.choice([None, 1, 2, 3])
        plan = plan_stream(holder, candidates, adds=adds, switchable=switchable, rehold_after=rehold_after)
        assert plan.exact
        assert plan.value == pytest.approx(brute_force(holder, candidates, adds, switchable, rehold_after))
        # The plan it reports is itself legal and adds up.
        assert len(plan.holders) == horizon and plan.adds <= (horizon if adds is None else adds)
        assert plan.value == pytest.approx(sum(s.value for s in plan.stretches))
        gap = horizon + 1 if rehold_after is None else rehold_after
        clears, prev = {}, HOLDER
        for d, pid in enumerate(plan.holders):
            if pid != prev:
                assert clears.get(pid, -1) <= d
                clears[prev] = d + gap
                prev = pid


@pytest.mark.unit
def test_a_two_week_horizon_with_a_full_pool_is_quick():
    """A two-week playoff round, sixty free agents, an add a day: well under a second."""
    rng = random.Random(7)
    horizon = 14

    def values():
        return tuple(float(rng.choice([0, 0, 0, 18, 22, 26, 31, 35])) for _ in range(horizon))

    holder = Candidate(HOLDER, values())
    candidates = [Candidate(100 + k, values(), rng.choice([0, 0, 0, 0, 2])) for k in range(60)]
    started = time.perf_counter()
    plan = plan_stream(holder, candidates, adds=None)
    assert time.perf_counter() - started < 1.0
    assert plan.exact and plan.value >= plan.hold_value and len(plan.holders) == horizon


@pytest.mark.unit
def test_every_other_day_schedules_are_still_solved_exactly_and_quickly():
    """The search's hardest shape: everyone plays every other day, so the program keeps wanting
    to take players straight back. With waivers in the way it must search; it stays exact."""
    rng = random.Random(1)
    horizon = 14

    def values():
        start = rng.randint(0, 1)
        return tuple(float(rng.choice([18, 22, 26, 31, 35])) if (d + start) % 2 == 0 else 0.0
                     for d in range(horizon))

    for _ in range(10):
        holder = Candidate(HOLDER, values())
        candidates = [Candidate(100 + k, values()) for k in range(40)]
        started = time.perf_counter()
        plan = plan_stream(holder, candidates, adds=None)
        assert time.perf_counter() - started < 1.0 and plan.exact
