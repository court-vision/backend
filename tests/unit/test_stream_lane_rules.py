"""
services.stream_lane_service's pure rules: the first day a pickup can count, the waiver gap
from the league's waiver period, the budget (limits, usage, pickups already scheduled), the
slots a pool player fills, the plan's inputs from a board and a pool (one spot, and several
planned together), the projections, and how a decision compares with the projection before it.
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
    plan_lanes,
    projections_of,
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
@pytest.mark.parametrize("hours, gap", [(24, 2), (None, 2), (0, 1), (12, 2), (48, 3), (72, 4)])
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
    assert budget.adds == 2 and budget.day_limits == (None, None, None)   # 4 - 1 used - 1 scheduled


@pytest.mark.unit
def test_a_season_cap_can_be_the_tighter_one():
    budget = lane_budget(acq(season_limit=10, season_used=9), [day(0, "DEN")], [])
    assert budget.adds == 1
    assert lane_budget(acq(limit=None, matchup_used=0, season_limit=None), [day(0)], []).adds is None


@pytest.mark.unit
def test_a_per_day_limit_leaves_each_day_its_room():
    days = [day(i, "DEN") for i in range(3)]
    daily = acq(per="day", limit=1, matchup_used=0)
    budget = lane_budget(daily, days, [pending(29)], first_day_adds=1)
    assert budget.adds is None and budget.day_limits == (0, 0, 1)
    assert lane_budget(daily, days, []).day_limits == (1, 1, 1)                 # unread: ESPN decides
    assert lane_budget(acq(per="day", limit=2), days, [pending(29)]).day_limits == (2, 1, 2)


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


def plan(pool, *, pending_=(), adds=None, limits=None, rehold=2, day_limits=(None, None, None), roster=ROSTER,
         holders=(HOLDER,), values=None):
    from services.stream_lane_service import Budget
    return plan_lanes(board(roster, limits=limits), list(holders), pool, DAYS, list(pending_), Budget(adds, day_limits),
                      rehold, values)


@pytest.mark.unit
def test_the_spot_is_worth_what_its_player_adds_to_each_days_lineup():
    p = plan([])
    # The holder plays days 0 and 2 into the open UT seat (the guard and the big have their own).
    assert p.marginals == ((15.0, 0.0, 15.0),) and p.spots == ((HOLDER,) * 3,)


@pytest.mark.unit
def test_a_free_agent_who_plays_the_holders_day_off_takes_it_with_one_add():
    p = plan([fa(10, "LAL")], adds=1)
    # One add: switching for day 1 gives up the holder's day 2 (15) for the free agent's 22.
    assert p.spots == ((HOLDER, 10, 10),) and p.value == 15 + 22 + 22
    assert p.considered == {10}


@pytest.mark.unit
def test_who_the_plan_may_not_take():
    pool = [
        fa(10, "LAL"),
        fa(11, "LAL", pos=5, positions=("C",)),          # a third C over the league's cap of two
        fa(12, "LAL", available_on=date(2026, 11, 19)),  # clears waivers after the horizon
        fa(13, "OKC"),                                   # never plays in it
        fa(GUARD, "DEN"),                                # on the roster
        fa(14, "PHX"),                                   # already scheduled
        fa(10, "LAL", value=99),                         # the pool listing a player twice: the first counts
    ]
    p = plan(pool, pending_=[pending(29, add=14)], limits={"5": 2}, adds=1)
    assert p.considered == {10} and p.value == 15 + 22 + 22


@pytest.mark.unit
def test_a_scheduled_pickup_that_fills_a_capped_position_closes_it_to_the_plan():
    """One C on the roster under a cap of two, and a pickup for day 2 adds a C (for the guard): a free-agent C
    taken for day 1 would have to leave again before day 2, for an add the budget doesn't have."""
    pool = [fa(11, "LAL", pos=5, positions=("C",)), fa(14, "PHX", pos=5, positions=("C",))]
    p = plan(pool, pending_=[pending(30, add=14, drop=GUARD)], limits={"5": 2}, roster=ROSTER[:3], adds=1)
    assert p.spots == ((HOLDER,) * 3,)
    # With a second add he takes days 0 and 1 (22 on day 1 for the holder's 15 on day 0), and the holder,
    # off waivers by day 2, takes the spot back for day 2 before the third C would arrive.
    p = plan(pool, pending_=[pending(30, add=14, drop=GUARD)], limits={"5": 2}, roster=ROSTER[:3], adds=2)
    assert p.spots == ((11, 11, HOLDER),) and p.value == 0 + 22 + 15


@pytest.mark.unit
def test_a_scheduled_pickup_that_drops_a_capped_position_opens_it_from_its_day():
    """At the cap of two Cs (the big and the hurt one on IR), a pickup for day 1 drops the big: a free-agent C
    may take the spot from day 1, not before."""
    pool = [fa(11, "LAL", pos=5, positions=("C",)), fa(15, "OKC")]
    p = plan(pool, pending_=[pending(29, add=15, drop=BIG)], limits={"5": 2}, adds=1)
    assert p.considered == {11} and p.spots == ((HOLDER, 11, 11),)


@pytest.mark.unit
def test_an_out_player_and_ir_are_worth_nothing():
    roster = [p if p.player_id != HOLDER else p.model_copy(update={"injury_status": "OUT"}) for p in ROSTER]
    assert plan([], roster=roster).marginals == ((0.0, 0.0, 0.0),)


@pytest.mark.unit
def test_a_scheduled_pickup_joins_the_rest_of_the_roster_on_its_day():
    """A UT-only pickup for day 2 takes the UT seat the holder would have had."""
    utility = fa(14, "MIA", value=40, positions=())
    assert plan([utility], pending_=[pending(30, add=14)]).marginals == ((15.0, 0.0, 0.0),)


@pytest.mark.unit
def test_closed_days_reach_the_planner():
    """Day 1 closed (its add is spent): the switch moves a day earlier, giving up the holder's 15
    on day 0 for both of the free agent's 22s — rather than waiting for day 2 and getting one."""
    p = plan([fa(10, "LAL")], adds=1, day_limits=(None, 0, None))
    assert p.spots == ((10, 10, 10),) and p.value == 0 + 22 + 22
    p = plan([fa(10, "LAL")], adds=1, day_limits=(0, 0, None))
    assert p.spots == ((HOLDER, HOLDER, 10),) and p.value == 15 + 0 + 22


# ---- several spots ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_spots_share_the_adds_and_one_free_agent_goes_to_one_of_them():
    """The holder and the big both stream. One add left: it goes where it gains most — the big sits
    days 1 and 2 (BOS plays day 0 only), so his spot takes the free agent; the holder keeps his."""
    roster = [ROSTER[0], ROSTER[1], ROSTER[2].model_copy(update={"team": "DEN"})]
    p = plan([fa(10, "LAL"), fa(11, "LAL")], roster=roster, holders=(HOLDER, BIG), adds=1)
    assert p.adds == 1 and p.spots[0] == (HOLDER,) * 3
    # The big plays day 0 only: his spot takes a free agent from day 1, whose LAL plays days 1 and 2.
    assert p.spots[1][0] == BIG and p.spots[1][1] in (10, 11) and p.spots[1][1] == p.spots[1][2]


@pytest.mark.unit
def test_two_spots_with_adds_to_spare_each_stream():
    roster = [ROSTER[0], ROSTER[1], ROSTER[2].model_copy(update={"team": "DEN"})]
    p = plan([fa(10, "LAL"), fa(11, "LAL", positions=("C",), pos=5)], roster=roster, holders=(HOLDER, BIG), adds=2)
    assert p.adds == 2 and {p.spots[0][1], p.spots[1][1]} == {10, 11}


# ---- the value function ----------------------------------------------------------------------


@pytest.mark.unit
def test_values_stand_in_for_the_boards_and_the_pools_and_a_player_left_out_is_worth_nothing():
    """Under a value function (blocks, the matchup), the plan reads each player's worth from it: the
    free agent worth 22 on the board is worth 3 to it, the holder 1, and the guard nothing at all."""
    values = {HOLDER: 1.0, 10: 3.0, BIG: 2.0}
    p = plan([fa(10, "LAL")], adds=1, values=values)
    assert p.spots == ((HOLDER, 10, 10),) and p.marginals == ((1.0, 3.0, 3.0),) and p.value == 1 + 3 + 3
    assert p.hold_value == 1 + 0 + 1 and p.considered == {10}
    # A free agent the function leaves out never takes the spot.
    p = plan([fa(10, "LAL")], adds=1, values={HOLDER: 1.0})
    assert p.spots == ((HOLDER,) * 3,) and p.considered == set()


@pytest.mark.unit
def test_the_joint_plan_takes_the_value_function_and_the_closed_days():
    values = {HOLDER: 1.0, GUARD: 1.0, 10: 9.0}
    p = plan([fa(10, "LAL")], holders=(HOLDER, GUARD), values=values, day_limits=(None, 0, None))
    # Day 1 is closed to every spot, so the free agent (9 on days 1 and 2 under the function) comes in today.
    assert (10, 10, 10) in p.spots and p.adds == 1 and p.value == 1 + 9 + 9
    # Today closed too: he comes in for day 2 only.
    p = plan([fa(10, "LAL")], holders=(HOLDER, GUARD), values=values, day_limits=(0, 0, None))
    assert any(spot[2] == 10 and spot[1] != 10 for spot in p.spots) and p.adds == 1


@pytest.mark.unit
def test_the_joint_plan_leaves_the_spots_the_positions_room():
    """One C allowed and the big holds it: a C free agent can only replace the big, never the holder."""
    big_fa = fa(12, "LAL", value=50, positions=("C",), pos=5)
    p = plan([big_fa], holders=(HOLDER, BIG), limits={"5": 1})
    assert p.spots == ((HOLDER, HOLDER, HOLDER), (BIG, 12, 12))
    # With room for two Cs, the holder's spot could take him too... but he holds one spot at a time.
    p = plan([big_fa], holders=(HOLDER, BIG), limits={"5": 2})
    assert sum(12 in spot for spot in p.spots) == 1


@pytest.mark.unit
def test_a_scheduled_pickup_and_the_roster_are_never_candidates_for_the_joint_plan():
    p = plan([fa(GUARD, "LAL", value=99), fa(900, "LAL", value=99)], pending_=[pending(29)])
    assert p.considered == set() and p.spots == ((HOLDER,) * 3,)


# ---- the projection --------------------------------------------------------------------------


NAMES = {HOLDER: LanePlayer(player_id=HOLDER, name="Holder", team="MIA"),
         10: LanePlayer(player_id=10, name="FA 10", team="LAL")}
AS_OF = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_the_projection_lists_each_days_holder_and_switch():
    p = plan([fa(10, "LAL")], adds=1)
    (proj,) = projections_of(p, DAYS, NAMES, adds_left=1, rehold_after=2, now=AS_OF)
    assert [(d.scoring_period_id, d.player.player_id, d.value, d.switch) for d in proj.days] == [
        (28, HOLDER, 15.0, False), (29, 10, 22.0, True), (30, 10, 22.0, False)]
    assert (proj.value, proj.hold_value, proj.gain, proj.adds, proj.adds_left) == (59.0, 30.0, 29.0, 1, 1)
    assert proj.lanes == 1


@pytest.mark.unit
def test_each_spots_projection_counts_its_own_switches_and_shares_the_budget():
    roster = [ROSTER[0], ROSTER[1], ROSTER[2].model_copy(update={"team": "DEN"})]
    p = plan([fa(10, "LAL")], roster=roster, holders=(HOLDER, BIG), adds=1)
    names = {**NAMES, BIG: LanePlayer(player_id=BIG, name="Big", team="DEN")}
    holder, big = projections_of(p, DAYS, names, adds_left=1, rehold_after=2, now=AS_OF)
    assert (holder.adds, big.adds, holder.lanes, big.adds_left) == (0, 1, 2, 1)
    assert holder.gain == 0 and big.gain == round(big.value - big.hold_value, 2) > 0
    assert [d.switch for d in big.days] == [False, True, False]


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
