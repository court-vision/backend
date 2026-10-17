"""
services.multi_stream_planner: several streamed spots planned together.

What one-at-a-time planning gets wrong and the joint plan must not: the spots share
the adds (a per-day limit across every spot, the budget), two streamers can compete
for one seat, a free agent can hold only one spot, and a player dropped from any
spot waits out their waivers before any spot takes them back. Points come first, then
fewer adds, then fewer switches today. Optimality is checked against brute force on
small horizons, and one spot against the one-spot planner's exact program.
"""

import itertools
import random
import time

import pytest

from services.multi_stream_planner import UNIT, Streamer, Switch, plan_streams
from services.stream_planner import Candidate, DayPlayer, best_lineup_value, plan_stream, spot_values

PG, SG, SF, PF, C, G, F, UT = 0, 1, 2, 3, 4, 5, 6, 11
STANDARD = {PG: 1, SG: 1, SF: 1, PF: 1, C: 1, G: 1, F: 1, UT: 3}


def s(pid, *values, eligible=(UT,), available_from=0, position=None):
    return Streamer(pid, tuple(float(v) for v in values), frozenset(eligible), available_from, position)


def empty(horizon):
    return [[] for _ in range(horizon)]


# ---- what the joint plan gets right ---------------------------------------------------------


@pytest.mark.unit
def test_the_spots_share_one_add_a_day():
    """Two spots, two free agents worth taking from day 0, one add a day across the team: the
    better one comes in today for the weaker spot, the other tomorrow."""
    holders = [s(1, 0, 0, 0), s(2, 5, 5, 5)]
    candidates = [s(3, 30, 30, 30), s(4, 20, 20, 20)]
    plan = plan_streams(holders, candidates, empty(3), {UT: 2}, adds=None, day_limits=[1, 1, 1])
    assert plan.spots == ((3, 3, 3), (2, 4, 4))
    assert (plan.value, plan.hold_value, plan.adds) == (135.0, 15.0, 2)
    assert plan.first_moves == (Switch(0, 0, 1, 3),)
    # Without the limit both come in today.
    plan = plan_streams(holders, candidates, empty(3), {UT: 2}, adds=None)
    assert len(plan.first_moves) == 2 and plan.value == 150.0


@pytest.mark.unit
def test_the_budget_is_shared_across_spots():
    holders = [s(1, 0, 0), s(2, 5, 5)]
    plan = plan_streams(holders, [s(3, 30, 30), s(4, 20, 20)], empty(2), {UT: 2}, adds=1)
    assert plan.spots == ((3, 3), (2, 2)) and plan.value == 70.0 and plan.adds == 1


@pytest.mark.unit
def test_two_streamers_for_one_seat_are_worth_less_than_apart():
    """One C seat and one G seat open: the two best free agents are both C-only, so together
    they're worth one of them. The plan takes a C and the guard."""
    holders = [s(1, 0, eligible=(C,)), s(2, 0, eligible=(C,))]
    candidates = [s(3, 30, eligible=(C,)), s(4, 28, eligible=(C,)), s(5, 20, eligible=(G,))]
    plan = plan_streams(holders, candidates, empty(1), {C: 1, G: 1}, adds=None)
    assert {plan.spots[0][0], plan.spots[1][0]} == {3, 5} and plan.value == 50.0
    # A run's value is what its player adds with the other spot's player in the lineup.
    assert sorted(st.value for spot in plan.stretches for st in spot) == [20.0, 30.0]


@pytest.mark.unit
def test_seats_the_rest_of_the_roster_can_move_out_of_count():
    """The rest's PG/G guard moves to G so a PG-only free agent can start at PG."""
    rest = [[DayPlayer(10, 25.0, frozenset({PG, G}))]]
    plan = plan_streams([s(1, 0, eligible=(G,))], [s(2, 18, eligible=(PG,))], rest, {PG: 1, G: 1}, adds=1)
    assert plan.spots == ((2,),) and plan.value == 18.0


@pytest.mark.unit
def test_a_free_agent_holds_one_spot():
    holders = [s(1, 0, 0), s(2, 0, 0)]
    plan = plan_streams(holders, [s(3, 40, 40), s(4, 10, 10)], empty(2), {UT: 2}, adds=None)
    assert {plan.spots[0][0], plan.spots[1][0]} == {3, 4} and plan.value == 100.0


@pytest.mark.unit
def test_a_dropped_player_waits_out_their_waivers():
    """B plays only day 1. Swapping the 20-point holder out for B's day (when they sit) would keep
    them on waivers through day 2, so the plan swaps the 5-point one, back for day 3. With one
    day of waivers the better swap works."""
    holders = [s(1, 20, 0, 20, 20), s(2, 5, 5, 5, 5)]
    b = s(3, 0, 15, 0, 0)
    plan = plan_streams(holders, [b], empty(4), {UT: 2}, adds=None)
    assert plan.spots == ((1, 1, 1, 1), (2, 3, 3, 2)) and plan.value == 85.0 and plan.adds == 2
    plan = plan_streams(holders, [b], empty(4), {UT: 2}, adds=None, rehold_after=1)
    assert plan.spots == ((1, 3, 1, 1), (2, 2, 2, 2)) and plan.value == 95.0


@pytest.mark.unit
def test_waiver_gaps_longer_than_two_days_are_planned():
    """Dropping the holder for days 1 and 2 pays only with them back for day 3's big game. Two
    days of waivers: dropped for day 1. Three: dropped a day early, giving up their 10. Four:
    they can't be back in time, and stay."""
    holder, a = s(1, 10, 0, 0, 50), s(2, 0, 20, 20, 0)
    for rehold_after, spots, value in ((2, (1, 2, 2, 1), 100.0), (3, (2, 2, 2, 1), 90.0), (4, (1, 1, 1, 1), 60.0)):
        plan = plan_streams([holder], [a], empty(4), {UT: 1}, adds=None, rehold_after=rehold_after)
        assert plan.spots == (spots,) and plan.value == value


@pytest.mark.unit
def test_a_position_limit_holds_across_spots():
    """Room for one C among the spots: two C free agents can't both come in. Two C holders already
    there can stay, and a C can replace a C."""
    holders = [s(1, 0, 0, position=C), s(2, 0, 0, position=PG)]
    candidates = [s(3, 30, 30, position=C), s(4, 25, 25, position=C), s(5, 10, 10, position=SF)]
    plan = plan_streams(holders, candidates, empty(2), {UT: 2}, adds=None, position_room={C: [1, 1]})
    assert {plan.spots[0][0], plan.spots[1][0]} == {3, 5}
    two_cs = [s(1, 1, 1, position=C), s(2, 1, 1, position=C)]
    assert plan_streams(two_cs, [], empty(2), {UT: 2}, adds=None, position_room={C: [1, 0]}).adds == 0
    plan = plan_streams(two_cs, [s(3, 30, 30, position=C)], empty(2), {UT: 2}, adds=None, position_room={C: [0, 0]})
    assert plan.value == 31.0 * 2 and plan.adds == 1     # a C for a C keeps the count


@pytest.mark.unit
def test_a_player_with_no_games_can_fill_a_spot_a_position_limit_empties():
    """A C worth 30 today fits under the C limit today but not tomorrow, and the guard they'd replace
    is still on waivers then: a free agent with no games takes the spot for tomorrow."""
    holders = [s(1, 5, 5, position=C), s(2, 0, 0, position=PG)]
    big, filler = s(3, 30, 0, position=C), s(4, 0, 0, position=PG)
    plan = plan_streams(holders, [big, filler], empty(2), {UT: 2}, adds=None, position_room={C: [2, 1]})
    assert plan.spots == ((1, 1), (3, 4)) and plan.value == 40.0 and plan.adds == 2
    # Without one, the big C can only replace the other C: 30 instead of 40.
    plan = plan_streams(holders, [big], empty(2), {UT: 2}, adds=None, position_room={C: [2, 1]})
    assert plan.spots == ((3, 3), (2, 2)) and plan.value == 30.0


@pytest.mark.unit
def test_points_come_first_then_fewer_adds_then_no_switch_today():
    holders = [s(1, 0, 0, 0), s(2, 0, 0, 0)]
    # Equal points (one seat): one add, a player for days 0 and 2, beats two.
    plan = plan_streams(holders, [s(3, 10, 0, 10), s(4, 10, 0, 0), s(5, 0, 0, 10)], empty(3), {UT: 1}, adds=None)
    assert plan.adds == 1 and plan.value == 20.0
    # Switching today or tomorrow is the same 10 (the free agent first plays tomorrow): wait.
    plan = plan_streams(holders, [s(3, 0, 10, 0)], empty(3), {UT: 2}, adds=None)
    assert plan.first_moves == () and plan.adds == 1
    # A difference of a whole UNIT is a difference.
    plan = plan_streams([s(1, 0)], [s(2, UNIT)], empty(1), {UT: 1}, adds=1)
    assert plan.spots == ((2,),)


@pytest.mark.unit
def test_nothing_to_spend_or_nobody_to_take_keeps_every_spot():
    holders = [s(1, 5, 5), s(2, 0, 7)]
    for plan in (plan_streams(holders, [s(3, 30, 30)], empty(2), {UT: 2}, adds=0),
                 plan_streams(holders, [], empty(2), {UT: 2}, adds=None),
                 plan_streams(holders, [s(3, 30, 30)], empty(2), {UT: 2}, adds=None, day_limits=[0, 0]),
                 plan_streams(holders, [s(3, 0, 0)], empty(2), {UT: 2}, adds=None)):
        assert plan.spots == ((1, 1), (2, 2)) and plan.adds == 0 and plan.gain == 0.0 and plan.optimal
        assert plan.value == plan.hold_value == 17.0


@pytest.mark.unit
def test_a_free_agent_on_waivers_waits_until_they_clear():
    plan = plan_streams([s(1, 0, 0, 0)], [s(2, 40, 40, 40, available_from=2)], empty(3), {UT: 1}, adds=None)
    assert plan.spots == ((1, 1, 2),)


@pytest.mark.unit
def test_bad_input_is_refused():
    with pytest.raises(ValueError):
        plan_streams([], [s(2, 1)], empty(1), {UT: 1}, adds=None)
    with pytest.raises(ValueError):
        plan_streams([s(1, 1), s(1, 1)], [], empty(1), {UT: 1}, adds=None)
    with pytest.raises(ValueError):
        plan_streams([s(1, 1, 1)], [s(2, 1)], empty(2), {UT: 1}, adds=None)
    with pytest.raises(ValueError):
        plan_streams([s(1, 1, 1)], [], empty(2), {UT: 1}, adds=None, day_limits=[1])
    with pytest.raises(ValueError):
        plan_streams([s(1, 1)], [], empty(1), {UT: 1}, adds=None, rehold_after=0)
    with pytest.raises(ValueError):
        plan_streams([s(1, 1)], [], empty(1), {UT: 1}, adds=None, position_room={C: [1, 1]})


# ---- optimality ----------------------------------------------------------------------------


def brute_force(holders, candidates, rest_by_day, slots, adds, day_limits, rehold_after, position_room=None):
    """Every legal sequence of who holds the spots each day, scored: (points in UNITs, -adds,
    -switches today), the best."""
    horizon, spots = len(rest_by_day), len(holders)
    start = frozenset(h.player_id for h in holders)
    players = [*holders, *(c for c in candidates if c.player_id not in start)]
    by_id = {p.player_id: p for p in players}
    sets = [frozenset(ids) for ids in itertools.combinations(sorted(by_id), spots)]
    bases = [best_lineup_value(rest, slots) for rest in rest_by_day]
    worth = {(d, ids): best_lineup_value([*rest_by_day[d], *(DayPlayer(p, by_id[p].values[d], by_id[p].eligible)
                                                              for p in ids)], slots) - bases[d]
             for d in range(horizon) for ids in sets}
    held_at = {pos: sum(1 for h in holders if h.position == pos) for pos in position_room or {}}
    best = None
    for seq in itertools.product(sets, repeat=horizon):
        prev, dropped, spent, today, ok = start, {}, 0, 0, True
        for d, ids in enumerate(seq):
            new = ids - prev
            cap = len(holders) if day_limits is None or day_limits[d] is None else day_limits[d]
            if len(new) > cap or any(d < by_id[p].available_from or d < dropped.get(p, -99) + rehold_after
                                     for p in new):
                ok = False
                break
            if any(sum(1 for p in ids if by_id[p].position == pos) > max(room[d], held_at[pos])
                   for pos, room in (position_room or {}).items()):
                ok = False
                break
            for p in prev - ids:
                dropped[p] = d
            spent += len(new)
            today += len(new) if d == 0 else 0
            prev = ids
        if ok and (adds is None or spent <= adds):
            key = (round(sum(worth[(d, ids)] for d, ids in enumerate(seq)) / UNIT), -spent, -today)
            best = key if best is None else max(best, key)
    return best


def key_of(plan):
    return round(plan.value / UNIT), -plan.adds, -len(plan.first_moves)


@pytest.mark.unit
def test_matches_brute_force_on_random_small_horizons():
    rng = random.Random(20261016)
    slot_sets = [{C: 1, G: 1, UT: 1}, {PG: 1, C: 1}, {UT: 2}, {G: 1, F: 1, UT: 1}]
    eligibilities = [(C,), (C, UT), (PG, G, UT), (PG, G), (SF, F, UT), (UT,), (G, F)]
    for _ in range(120):
        horizon = rng.randint(1, 4)
        spots = rng.choice([2, 2, 3])
        n = rng.randint(0, 3)

        def values():
            return [float(rng.choice([0, 0, 10, 15, 20, 25])) for _ in range(horizon)]

        def streamer(pid, available_from=0):
            return s(pid, *values(), eligible=rng.choice(eligibilities), available_from=available_from,
                     position=rng.choice([C, PG, SF]))

        slots = rng.choice(slot_sets)
        rest = [[DayPlayer(100 + i, float(rng.choice([0, 12, 18, 30])), frozenset(rng.choice(eligibilities)))
                 for i in range(rng.randint(0, 2))] for _ in range(horizon)]
        holders = [streamer(1 + k) for k in range(spots)]
        candidates = [streamer(10 + k, rng.choice([0, 0, 1, 2])) for k in range(n)]
        adds = rng.choice([None, 0, 1, 2, 3])
        day_limits = rng.choice([None, [rng.choice([None, 0, 1, 1, 2]) for _ in range(horizon)]])
        rehold_after = rng.choice([1, 2, 3])
        room = rng.choice([None, {C: [rng.choice([0, 1, 2]) for _ in range(horizon)]}])
        plan = plan_streams(holders, candidates, rest, slots, adds=adds, day_limits=day_limits,
                            rehold_after=rehold_after, position_room=room)
        assert plan.optimal
        assert key_of(plan) == brute_force(holders, candidates, rest, slots, adds, day_limits, rehold_after, room)
        assert_legal(plan, holders, candidates, adds, day_limits, rehold_after)


def assert_legal(plan, holders, candidates, adds, day_limits, rehold_after):
    """The plan it reports keeps every rule and adds up."""
    horizon = len(plan.day_values)
    by_id = {p.player_id: p for p in [*candidates, *holders]}
    assert plan.adds == len(plan.switches) and (adds is None or plan.adds <= adds)
    assert plan.value == pytest.approx(sum(plan.day_values))
    dropped: dict[int, int] = {}
    prev = [h.player_id for h in holders]
    for d in range(horizon):
        now = [plan.spots[k][d] for k in range(len(holders))]
        assert len(set(now)) == len(now)
        new = set(now) - set(prev)
        cap = len(holders) if day_limits is None or day_limits[d] is None else day_limits[d]
        assert len(new) <= cap and len([sw for sw in plan.switches if sw.day == d]) == len(new)
        for p in new:
            assert d >= by_id[p].available_from and d >= dropped.get(p, -99) + rehold_after
        for p in set(prev) - set(now):
            dropped[p] = d
        prev = now


@pytest.mark.unit
def test_one_spot_matches_the_one_spot_planner():
    """With one spot the joint program is the one-spot problem: the same points, the same adds,
    and the same answer to "switch today?"."""
    rng = random.Random(20261017)
    eligibilities = [(PG, G, UT), (SG, G, UT), (SF, F, UT), (PF, F, UT), (C, UT), (PF, C, F, UT)]
    for _ in range(40):
        horizon = rng.choice([3, 5, 7])

        def values():
            days = set(rng.sample(range(horizon), rng.randint(1, horizon)))
            return [float(rng.choice([10, 15, 20, 25, 30])) if d in days else 0.0 for d in range(horizon)]

        rest_players = [(values(), frozenset(rng.choice(eligibilities))) for _ in range(rng.randint(5, 9))]
        rest = [[DayPlayer(100 + i, v[d], e) for i, (v, e) in enumerate(rest_players)] for d in range(horizon)]
        holder = s(1, *values(), eligible=rng.choice(eligibilities))
        candidates = [s(10 + k, *values(), eligible=rng.choice(eligibilities), available_from=rng.choice([0, 0, 2]))
                      for k in range(rng.randint(2, 12))]
        adds = rng.choice([None, 1, 2, 3])
        switchable = [rng.random() > 0.2 for _ in range(horizon)]
        rehold_after = rng.choice([1, 2])

        def one(p):
            return Candidate(p.player_id, spot_values(rest, STANDARD, [DayPlayer(p.player_id, v, p.eligible)
                                                                       for v in p.values]), p.available_from)

        expected = plan_stream(one(holder), [one(c) for c in candidates], adds=adds, switchable=switchable,
                               rehold_after=rehold_after)
        plan = plan_streams([holder], candidates, rest, STANDARD, adds=adds,
                            day_limits=[None if ok else 0 for ok in switchable], rehold_after=rehold_after)
        assert (plan.value, plan.adds) == (pytest.approx(expected.value), expected.adds)
        assert plan.hold_value == pytest.approx(expected.hold_value)
        assert bool(plan.first_moves) == (expected.first_move is not None)
        assert sum(st.value for st in plan.stretches[0]) == pytest.approx(plan.value)


@pytest.mark.unit
def test_the_same_input_gives_the_same_plan():
    rng = random.Random(3)
    horizon = 7
    candidates = [s(100 + k, *(float(rng.choice([0, 0, 20, 20, 25])) for _ in range(horizon)))
                  for k in range(40)]
    holders = [s(1, *([10.0] * horizon)), s(2, *([5.0] * horizon)), s(3, *([0.0] * horizon))]
    plans = [plan_streams(holders, candidates, empty(horizon), {UT: 3}, adds=4) for _ in range(3)]
    assert plans[0] == plans[1] == plans[2]


@pytest.mark.unit
def test_a_two_week_round_with_a_full_pool_and_three_spots_is_quick():
    """150 free agents, three spots, an add a day across them: proven optimal in well under a
    second; the limit only catches a model that has stopped scaling, not a slow CI host."""
    rng = random.Random(7)
    horizon = 14
    eligibilities = [(PG, G, UT), (SG, G, UT), (PG, SG, G, UT), (SF, F, UT), (PF, F, UT), (C, UT), (PF, C, F, UT)]
    schedules = [{d for d in range(horizon) if rng.random() < 0.5} for _ in range(30)]

    def player(pid, value, available_from=0):
        team = rng.randrange(30)
        return s(pid, *(round(value, 1) if d in schedules[team] else 0.0 for d in range(horizon)),
                 eligible=rng.choice(eligibilities), available_from=available_from)

    keep = [player(100 + i, rng.uniform(22, 45)) for i in range(10)]
    rest = [[DayPlayer(p.player_id, p.values[d], p.eligible) for p in keep] for d in range(horizon)]
    holders = [player(1 + k, rng.uniform(15, 22)) for k in range(3)]
    candidates = [player(1000 + k, max(5.0, rng.gauss(17, 5)), rng.choice([0, 0, 0, 0, 2])) for k in range(150)]
    started = time.perf_counter()
    plan = plan_streams(holders, candidates, rest, STANDARD, adds=horizon, day_limits=[1] * horizon)
    assert time.perf_counter() - started < 10.0
    assert plan.optimal and plan.value >= plan.hold_value and plan.adds <= horizon
    assert_legal(plan, holders, candidates, horizon, [1] * horizon, 2)


@pytest.mark.unit
def test_a_plan_not_proven_in_time_is_returned_as_found():
    """Out of time, the best plan found so far comes back, marked unproven: at worst, keeping
    every spot's player."""
    rng = random.Random(11)
    horizon = 14
    candidates = [s(1000 + k, *(float(rng.choice([0, 0, 20, 25])) for _ in range(horizon))) for k in range(150)]
    holders = [s(1, *([5.0] * horizon)), s(2, *([0.0] * horizon))]
    plan = plan_streams(holders, candidates, empty(horizon), {UT: 2}, adds=5, time_limit=0.0)
    assert not plan.optimal and plan.value >= plan.hold_value == 70.0
    assert_legal(plan, holders, candidates, 5, None, 2)
