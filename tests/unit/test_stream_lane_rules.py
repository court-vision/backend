"""
services.stream_lane_service's pure rules: the first day a pickup can count, the waiver gap
from the league's waiver period, the budget (limits, usage, pickups already scheduled), the
slots a pool player fills, the plan's inputs from a board and a pool, the projection, and how
a decision compares with the projection before it.
"""

from datetime import date, datetime, timezone

import pytest

from schemas.common import FantasyProvider
from schemas.lineup_editor import AcquisitionState, LineupPlayer, LineupState
from schemas.stream_lane import LanePlayer
from services.stream_lane_service import (
    LaneDay,
    PendingPickup,
    PoolPlayer,
    active_slots,
    compare_to_projection,
    first_open_period,
    lane_budget,
    plan_lane,
    projection_of,
    rehold_after_for,
)

PG, SG, SF, PF, C, G, F, UT, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13
D0 = date(2026, 11, 16)                    # a Monday; ESPN day 28
TIP = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)    # 7 PM EST


def day(i, *teams, tip=TIP):
    return LaneDay(28 + i, date(2026, 11, 16 + i), frozenset(teams), tip if teams else None)


def acq(**kw):
    base = dict(per="matchup", limit=4, matchup_period_id=5, matchup_start=date(2026, 11, 16),
                matchup_end=date(2026, 11, 22), matchup_used=1, waiver_hours=24)
    base.update(kw)
    return AcquisitionState(**base)


def pending(period, *, add=900, drop=None, nba_date=None):
    return PendingPickup(1, period, nba_date or date(2026, 11, 16 + period - 28), add, drop)


# ---- timing --------------------------------------------------------------------------------


@pytest.mark.unit
def test_an_add_counts_for_today_until_its_first_tip():
    today = day(0, "DEN")
    assert first_open_period(today, datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)) == 28   # 6 PM EST
    assert first_open_period(today, TIP) == 29                           # at the tip: tomorrow
    assert first_open_period(day(0), TIP) == 28                          # nobody plays: today is open


@pytest.mark.unit
@pytest.mark.parametrize("hours, gap", [(24, 2), (None, 2), (0, 1), (12, 2), (48, None)])
def test_the_waiver_gap_follows_the_league(hours, gap):
    assert rehold_after_for(acq(waiver_hours=hours)) == gap


@pytest.mark.unit
def test_no_settings_mean_a_day_of_waivers():
    assert rehold_after_for(None) == 2


# ---- the budget ----------------------------------------------------------------------------


@pytest.mark.unit
def test_a_matchup_limit_leaves_its_room_less_what_is_scheduled_inside_the_matchup():
    days = [day(i, "DEN") for i in range(3)]
    inside, next_week = pending(29), pending(36, nba_date=date(2026, 11, 23))
    budget = lane_budget(acq(), days, [inside, next_week])
    assert budget.adds == 2 and budget.switchable == (True, True, True)   # 4 - 1 used - 1 scheduled


@pytest.mark.unit
def test_a_season_cap_can_be_the_tighter_one():
    budget = lane_budget(acq(season_limit=10, season_used=9), [day(0, "DEN")], [])
    assert budget.adds == 1
    assert lane_budget(acq(limit=None, matchup_used=0, season_limit=None), [day(0)], []).adds is None


@pytest.mark.unit
def test_a_per_day_limit_closes_days_already_spent():
    days = [day(i, "DEN") for i in range(3)]
    daily = acq(per="day", limit=1, matchup_used=0)
    budget = lane_budget(daily, days, [pending(29)], first_day_adds=1)
    assert budget.adds is None and budget.switchable == (False, False, True)
    assert lane_budget(daily, days, []).switchable == (True, True, True)       # unread: ESPN decides


@pytest.mark.unit
def test_no_limits_mean_no_budget():
    assert lane_budget(None, [day(0)], [pending(28)]) == lane_budget(None, [day(0)], [])


# ---- slots ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_combo_slots_follow_from_base_positions():
    assert active_slots(["PG", "G", "UT1", "UT2"]) == {PG, G, 8, UT}           # G/F
    assert active_slots(["SF", "PF", "F"]) == {SF, PF, F, 7, 8, 9, 10, UT}    # SG/SF, G/F, PF/C, F/C
    assert active_slots(["C"]) == {C, 9, 10, UT}


# ---- the plan ------------------------------------------------------------------------------


def player(pid, name, slot, team, *, eligible, avg=20.0, injury=None, pos=None):
    return LineupPlayer(player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot),
                        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible],
                        avg_points=avg, injury_status=injury, default_position_id=pos)


def board(players, *, limits=None):
    return LineupState(provider=FantasyProvider.ESPN, team_name="T", nba_date="2026-11-16", scoring_period_id=28,
                       slot_counts={"0": 1, "4": 1, "11": 1, "12": 3, "13": 1}, players=players,
                       position_limits=limits or {}, roster_version="v", fetched_at="now", can_write=True)


HOLDER, GUARD, BIG, HURT = 1, 2, 3, 4
ROSTER = [
    player(HOLDER, "Holder", UT, "MIA", eligible=(SF, UT, BE), avg=15, pos=3),
    player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE), avg=30, pos=1),
    player(BIG, "Big", C, "BOS", eligible=(C, UT, BE), avg=25, pos=5),
    player(HURT, "Hurt", IR, "BOS", eligible=(C, UT, BE, IR), avg=40, injury="OUT", pos=5),
]
DAYS = [day(0, "MIA", "DEN"), day(1, "LAL"), day(2, "MIA", "LAL")]


def fa(pid, team, *, value=22.0, positions=("SF", "F"), pos=3, available_on=None):
    return PoolPlayer(pid, f"FA {pid}", team, active_slots([*positions, "UT1"]), value, pos, available_on)


def plan(pool, *, pending_=(), adds=None, limits=None, rehold=2, switchable=(True, True, True), roster=ROSTER,
         values=None):
    from services.stream_lane_service import Budget
    return plan_lane(board(roster, limits=limits), HOLDER, pool, DAYS, list(pending_), Budget(adds, switchable), rehold,
                     values)


@pytest.mark.unit
def test_the_spot_is_worth_what_its_player_adds_to_each_days_lineup():
    lp = plan([])
    # The holder plays days 0 and 2 into the open UT seat (the guard and the big have their own).
    assert lp.values[HOLDER] == (15.0, 0.0, 15.0) and lp.plan.holders == (HOLDER,) * 3


@pytest.mark.unit
def test_a_free_agent_who_plays_the_holders_day_off_takes_it_with_one_add():
    lp = plan([fa(10, "LAL")], adds=1)
    # One add: switching for day 1 gives up the holder's day 2 (15) for the free agent's 22.
    assert lp.plan.holders == (HOLDER, 10, 10) and lp.plan.value == 15 + 22 + 22
    assert lp.considered == {10}


@pytest.mark.unit
def test_who_the_plan_may_not_take():
    pool = [
        fa(10, "LAL"),
        fa(11, "LAL", pos=5, positions=("C",)),          # a third C over the league's cap of two
        fa(12, "LAL", available_on=date(2026, 11, 19)),  # clears waivers after the horizon
        fa(13, "OKC"),                                   # never plays in it
        fa(GUARD, "DEN"),                                # on the roster
        fa(14, "PHX"),                                   # already scheduled
    ]
    lp = plan(pool, pending_=[pending(29, add=14)], limits={"5": 2})
    assert lp.considered == {10}


@pytest.mark.unit
def test_a_scheduled_pickup_that_fills_a_capped_position_closes_it_to_the_plan():
    """One C on the roster under a cap of two, and a pickup for day 2 adds a C (for the guard): a free-agent C
    taken before then would still hold the spot on day 2, as a third C ESPN refuses."""
    pool = [fa(11, "LAL", pos=5, positions=("C",)), fa(14, "PHX", pos=5, positions=("C",))]
    lp = plan(pool, pending_=[pending(30, add=14, drop=GUARD)], limits={"5": 2}, roster=ROSTER[:3], adds=1)
    assert lp.considered == frozenset() and lp.plan.holders == (HOLDER,) * 3


@pytest.mark.unit
def test_a_scheduled_pickup_that_drops_a_capped_position_opens_it_from_its_day():
    """At the cap of two Cs (the big and the hurt one on IR), a pickup for day 1 drops the big: a free-agent C
    may take the spot from day 1, not before."""
    pool = [fa(11, "LAL", pos=5, positions=("C",)), fa(15, "OKC")]
    lp = plan(pool, pending_=[pending(29, add=15, drop=BIG)], limits={"5": 2}, adds=1)
    assert lp.considered == {11} and lp.plan.holders == (HOLDER, 11, 11)


@pytest.mark.unit
def test_an_out_player_and_ir_are_worth_nothing():
    roster = [p if p.player_id != HOLDER else p.model_copy(update={"injury_status": "OUT"}) for p in ROSTER]
    lp = plan([], roster=roster)
    assert lp.values[HOLDER] == (0.0, 0.0, 0.0)


@pytest.mark.unit
def test_a_scheduled_pickup_joins_the_rest_of_the_roster_on_its_day():
    """A UT-only pickup for day 2 takes the UT seat the holder would have had."""
    utility = fa(14, "MIA", value=40, positions=())
    lp = plan([utility], pending_=[pending(30, add=14)])
    assert lp.values[HOLDER] == (15.0, 0.0, 0.0)


@pytest.mark.unit
def test_closed_days_reach_the_planner():
    """Day 1 closed (its add is spent): the switch moves a day earlier, giving up the holder's 15
    on day 0 for both of the free agent's 22s — rather than waiting for day 2 and getting one."""
    lp = plan([fa(10, "LAL")], adds=1, switchable=(True, False, True))
    assert lp.plan.holders == (10, 10, 10) and lp.plan.value == 0 + 22 + 22
    lp = plan([fa(10, "LAL")], adds=1, switchable=(False, False, True))
    assert lp.plan.holders == (HOLDER, HOLDER, 10) and lp.plan.value == 15 + 0 + 22


# ---- the projection --------------------------------------------------------------------------


NAMES = {HOLDER: LanePlayer(player_id=HOLDER, name="Holder", team="MIA"),
         10: LanePlayer(player_id=10, name="FA 10", team="LAL")}
AS_OF = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_the_projection_lists_each_days_holder_and_switch():
    lp = plan([fa(10, "LAL")], adds=1)
    proj = projection_of(lp, DAYS, NAMES, adds_left=1, rehold_after=2, now=AS_OF)
    assert [(d.scoring_period_id, d.player.player_id, d.value, d.switch) for d in proj.days] == [
        (28, HOLDER, 15.0, False), (29, 10, 22.0, True), (30, 10, 22.0, False)]
    assert (proj.value, proj.hold_value, proj.gain, proj.adds, proj.adds_left) == (59.0, 30.0, 29.0, 1, 1)


def _shown(*holders):
    return {"days": [{"scoring_period_id": 28 + i, "player": NAMES[h].model_dump(mode="json")}
                     for i, h in enumerate(holders)]}


@pytest.mark.unit
def test_a_decision_compared_with_the_projection_before_it():
    shown = _shown(HOLDER, 10, 10)
    assert compare_to_projection(shown, 29, 10, HOLDER, frozenset({10}))[1] == "as_projected"
    who, why = compare_to_projection(shown, 29, 11, HOLDER, frozenset({11}))
    assert (who.player_id, why) == (10, "projected_gone")           # 10 is no longer one it could take
    assert compare_to_projection(shown, 29, 11, HOLDER, frozenset({10, 11}))[1] == "replanned"
    assert compare_to_projection(shown, 28, 10, HOLDER, frozenset({10}))[1] == "replanned"   # kept, now switches
    assert compare_to_projection(None, 29, 10, HOLDER, frozenset()) == (None, None)
    assert compare_to_projection(shown, 31, 10, HOLDER, frozenset()) == (None, None)


# ---- the value function and the joint plan ------------------------------------------------


@pytest.mark.unit
def test_values_stand_in_for_the_boards_and_the_pools_and_a_player_left_out_is_worth_nothing():
    """Under a value function (blocks, the matchup), the plan reads each player's worth from it: the
    free agent worth 22 on the board is worth 3 to it, the holder 1, and the guard nothing at all."""
    values = {HOLDER: 1.0, 10: 3.0, BIG: 2.0}
    lp = plan([fa(10, "LAL")], adds=1, values=values)
    assert lp.values[HOLDER] == (1.0, 0.0, 1.0) and lp.values[10] == (0.0, 3.0, 3.0)
    assert lp.plan.holders == (HOLDER, 10, 10) and lp.plan.value == 1 + 3 + 3
    # A free agent the function leaves out never takes the spot.
    assert plan([fa(10, "LAL")], adds=1, values={HOLDER: 1.0}).plan.holders == (HOLDER,) * 3


def spots(holder_ids, pool, *, pending_=(), adds=None, limits=None, rehold=2, switchable=(True, True, True),
          roster=ROSTER, values=None):
    from services.stream_lane_service import Budget, plan_spots
    return plan_spots(board(roster, limits=limits), holder_ids, pool, DAYS, list(pending_), Budget(adds, switchable),
                      rehold, values)


@pytest.mark.unit
def test_two_spots_are_planned_together_from_the_same_inputs():
    """The holder and the guard stream together with one add: the guard (30 on day 0, no game after) gives
    his spot to the 35-point guard from day 1, and the holder keeps his day 2."""
    sp = spots([HOLDER, GUARD], [fa(10, "LAL", value=22), fa(11, "LAL", value=35, positions=("PG", "G"), pos=1)], adds=1)
    assert sp.plan.holder_ids == (HOLDER, GUARD) and sp.plan.adds == 1
    assert sp.plan.spots == ((HOLDER, HOLDER, HOLDER), (GUARD, 11, 11))
    assert sp.values[11] == (0.0, 35.0, 35.0) and sp.values[GUARD] == (30.0, 0.0, 0.0)
    assert sp.considered == {10, 11}
    assert sp.plan.value == (15 + 30) + 35 + (15 + 35) and sp.plan.hold_value == 15 + 30 + 15


@pytest.mark.unit
def test_the_joint_plan_takes_the_value_function_and_the_closed_days():
    values = {HOLDER: 1.0, GUARD: 1.0, 10: 9.0}
    sp = spots([HOLDER, GUARD], [fa(10, "LAL")], values=values, switchable=(True, False, True))
    # Day 1 is closed to every spot, so the free agent (9 on days 1 and 2 under the function) comes in today.
    assert (10, 10, 10) in sp.plan.spots and sp.plan.adds == 1
    assert sp.values[10] == (0.0, 9.0, 9.0) and sp.values[HOLDER] == (1.0, 0.0, 1.0)
    # Today closed too: he comes in for day 2 only.
    sp = spots([HOLDER, GUARD], [fa(10, "LAL")], values=values, switchable=(False, False, True))
    assert any(spot[2] == 10 and spot[1] != 10 for spot in sp.plan.spots) and sp.plan.adds == 1


@pytest.mark.unit
def test_the_joint_plan_leaves_the_spots_the_positions_room():
    """One C allowed and the big holds it: a C free agent can only replace the big, never the holder."""
    big_fa = fa(12, "LAL", value=50, positions=("C",), pos=5)
    sp = spots([HOLDER, BIG], [big_fa], limits={"5": 1})
    assert sp.plan.spots[0] == (HOLDER, HOLDER, HOLDER) and sp.plan.spots[1] == (BIG, 12, 12)
    # With room for two Cs, the holder's spot takes him too... but he holds one spot at a time.
    sp = spots([HOLDER, BIG], [big_fa], limits={"5": 2})
    assert sum(12 in spot for spot in sp.plan.spots) == 1


@pytest.mark.unit
def test_a_scheduled_pickup_and_the_roster_are_never_candidates_for_the_joint_plan():
    sp = spots([HOLDER], [fa(GUARD, "LAL", value=99), fa(900, "LAL", value=99)], pending_=[pending(29)])
    assert sp.considered == set()
