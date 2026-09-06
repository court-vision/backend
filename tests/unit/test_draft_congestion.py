"""
Slot congestion: what a candidate benches on the roster's real game nights.

Fabricated calendars throughout — a week here is a tuple of "who plays today"
sets, and rosters are a handful of players with hand-set values, so every
benched number below can be checked on paper. The board wiring (which players,
which slots, which weeks) is `test_draft_board_service.py`'s.
"""

import pytest

from services.draft_congestion import (
    DEFAULT_SEASON_WEEKS,
    NO_SCHEDULE,
    NO_SLOTS,
    NO_TEAM,
    CongestionPlayer,
    SampleWeek,
    Stack,
    active_slots,
    benched_value,
    build_congestion_model,
    eligible_slots,
    max_weight_assignment,
    week_from_calendar,
)

pytestmark = pytest.mark.unit

FULL = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1}
GUARD = ("PG", "G", "UT")


def _p(id, value, team="DEN", slots=GUARD, position=None):
    return CongestionPlayer(
        id=id, value=value, team=team,
        slots=frozenset(slots) if slots is not None else None, position=position,
    )


def _week(number, *days):
    return SampleWeek(number=number, days=tuple(frozenset(d) for d in days))


ONE_DEN_NIGHT = (_week(3, {"DEN"}),)


def _model(roster, roster_slots=FULL, weeks=ONE_DEN_NIGHT, season_weeks=1):
    """Unscaled by default (one season week per sampled week), so a penalty
    reads as the raw benched value over the sample."""
    return build_congestion_model(roster, roster_slots, weeks, season_weeks)


# ---- vocabulary ------------------------------------------------------------


def test_active_slots_expands_counts_and_drops_bench_and_ir():
    assert active_slots(FULL) == ("PG", "SG", "SF", "PF", "C", "G", "F", "UT", "UT", "UT")
    assert active_slots({}) == ()
    assert active_slots(None) == ()
    # A count that is not a positive number is no seat; bench and IR never are.
    assert active_slots({"UT": 0, "C": "x", "BE": 3, "IR": 1, "": 2}) == ()
    assert active_slots({"C": "2"}) == ("C", "C")


def test_week_from_calendar_reads_only_the_days_inside_the_span():
    week = week_from_calendar(
        3, 3, {"DEN": {"0": True, "2": True, "5": True}, "BOS": {"1": True}, "LAL": {}}
    )
    assert week.number == 3
    assert week.days == (frozenset({"DEN"}), frozenset({"BOS"}), frozenset({"DEN"}))
    # Integer day keys are tolerated; an idle team is simply absent.
    assert week_from_calendar(1, 2, {"DEN": {0: True}}).days == (frozenset({"DEN"}), frozenset())


def test_eligibility_reads_espn_slots_then_the_position_then_everything():
    active = ("PG", "G", "UT", "UT")
    # ESPN's slots are authoritative, empty intersection included.
    assert eligible_slots(_p(1, 10, slots={"C", "UT"}), active) == {"UT"}
    assert eligible_slots(_p(5, 10, slots=()), active) == frozenset()
    # Without them the primary position picks the slots that admit it...
    assert eligible_slots(_p(2, 10, slots=None, position="PG"), active) == {"PG", "G", "UT"}
    assert eligible_slots(_p(3, 10, slots=None, position="C"), active) == {"UT"}
    # ...and without that, missing data is never a penalty: every seat.
    assert eligible_slots(_p(4, 10, slots=None), active) == {"PG", "G", "UT"}


# ---- the matching ----------------------------------------------------------


def test_three_guards_for_two_seats_bench_the_cheapest():
    players = [_p(1, 10), _p(2, 8), _p(3, 6)]
    seats = ("PG", "UT")
    assert max_weight_assignment(players, seats) == {1, 2}
    assert benched_value(players, seats, ONE_DEN_NIGHT) == 6.0
    assert benched_value(players, seats, (_week(3, {"DEN"}, {"DEN"}),)) == 12.0
    # A night nobody on the roster plays costs nothing.
    assert benched_value(players, seats, (_week(3, {"DEN"}, {"BOS"}),)) == 6.0


def test_augmenting_paths_seat_players_a_first_fit_greedy_would_bench():
    # A takes the only seat B can use; B is seated by moving A to his other one.
    a, b = _p(1, 10, slots={"PG", "G"}), _p(2, 9, slots={"G"})
    assert max_weight_assignment([a, b], ("PG", "G")) == {1, 2}
    # A chain of two moves.
    a = _p(1, 10, slots={"PG", "SG"})
    b = _p(2, 9, slots={"SG", "UT"})
    c = _p(3, 8, slots={"PG"})
    assert max_weight_assignment([a, b, c], ("PG", "SG", "UT")) == {1, 2, 3}
    # Capacity is capacity: the better player keeps the one seat.
    assert max_weight_assignment([_p(1, 10, slots={"PG"}), _p(2, 9, slots={"PG"})], ("PG",)) == {1}
    # A player who can start nowhere is never seated, whatever his value.
    assert max_weight_assignment([_p(1, 99, slots=())], ("PG", "UT")) == frozenset()
    assert max_weight_assignment([], ("PG",)) == frozenset()
    assert max_weight_assignment([_p(1, 10)], ()) == frozenset()


# ---- the model -------------------------------------------------------------


def test_an_empty_roster_benches_nothing_and_penalizes_nobody():
    model = _model([])
    assert model.benched_per_week == 0.0 and model.benched_season == 0.0
    assert model.stacks == () and model.no_team == ()
    pen = model.penalty(_p(1, 10))
    assert pen.value == 0.0 and pen.reason is None
    assert pen.games == 1 and pen.stack == 0
    assert str(pen.value) == "0.0"          # never -0.0


def test_a_fifth_player_on_a_stacked_team_costs_more_than_an_equal_one_elsewhere():
    """Four Nuggets fill G/F/UT/UT on every Denver night only by re-seating
    (the first two are both G-eligible); a fifth cannot be seated at all, and a
    Celtic of the same value plays the nights nobody else does."""
    roster = [
        _p(11, 10, slots={"PG", "G", "UT"}), _p(12, 10, slots={"SG", "G", "UT"}),
        _p(13, 10, slots={"SF", "F", "UT"}), _p(14, 10, slots={"PF", "F", "UT"}),
    ]
    week = (_week(3, {"DEN"}, {"DEN"}, {"DEN"}, {"BOS"}, {"BOS"}),)
    model = _model(roster, roster_slots={"G": 1, "F": 1, "UT": 2}, weeks=week)
    assert model.benched_per_week == 0.0
    assert model.stacks == (Stack("DEN", 4, (11, 12, 13, 14)),)

    nugget = model.penalty(_p(20, 8, "DEN", slots={"PG", "G", "UT"}))
    assert nugget.value == -24.0 and nugget.per_week == 24.0
    assert nugget.games == 3 and nugget.stack == 4

    celtic = model.penalty(_p(21, 8, "BOS", slots={"PG", "G", "UT"}))
    assert celtic.value == 0.0 and celtic.games == 2 and celtic.stack == 0

    # It is the calendar that decides, not the label: a Celtic whose team
    # plays the Denver nights collides exactly like a fifth Nugget.
    shared = (_week(3, {"DEN", "BOS"}, {"DEN", "BOS"}, {"DEN", "BOS"}),)
    same_nights = _model(roster, roster_slots={"G": 1, "F": 1, "UT": 2}, weeks=shared)
    assert same_nights.penalty(_p(21, 8, "BOS", slots={"PG", "G", "UT"})).value == -24.0


def test_a_roster_of_point_guards_benches_the_cheapest_one():
    roster = [_p(i, value) for i, value in ((1, 10), (2, 9), (3, 8), (4, 7), (5, 6))]
    model = _model(roster)                  # PG, G, UT x3: five seats for five guards
    assert model.benched_per_week == 0.0
    # A sixth, cheaper guard benches himself; a better one benches the cheapest
    # incumbent; a centre finds the centre seat empty.
    assert model.penalty(_p(30, 5)).value == -5.0
    assert model.penalty(_p(31, 12)).value == -6.0
    assert model.penalty(_p(32, 5, slots={"C", "UT"})).value == 0.0


def test_the_penalty_is_never_positive():
    seats = {"PG": 1, "UT": 1}
    model = _model([_p(1, 10), _p(2, 8)], roster_slots=seats)
    cases = [
        (_p(20, 50), -8.0),                  # a star benches the cheapest incumbent, not himself
        (_p(21, 7, slots=()), -7.0),         # a player who starts nowhere benches himself
        (_p(22, 3, "BOS"), 0.0),             # plays a night nobody else does: nothing benched
    ]
    for candidate, expected in cases:
        pen = model.penalty(candidate)
        assert pen.value == expected
        assert 0.0 >= pen.value >= -(candidate.value * pen.games * model.scale)
    # Adding to an empty lineup benches nothing, reported as a clean zero.
    assert str(_model([], roster_slots=seats).penalty(_p(1, 10)).value) == "0.0"


def test_the_sample_scales_to_the_season():
    seats = {"PG": 1, "UT": 1}
    model = _model([_p(1, 10), _p(2, 8)], roster_slots=seats, season_weeks=24)
    assert model.scale == 24.0
    pen = model.penalty(_p(3, 6))
    assert pen.value == -144.0 and pen.per_week == 6.0
    # Two identical sampled weeks measure the same per-week rate, and the
    # season number does not double with the sample.
    two = _model([_p(1, 10), _p(2, 8)], roster_slots=seats,
                 weeks=(_week(3, {"DEN"}), _week(9, {"DEN"})), season_weeks=24)
    assert two.scale == 12.0 and two.sample_weeks == (3, 9)
    assert two.penalty(_p(3, 6)).value == -144.0 and two.penalty(_p(3, 6)).per_week == 6.0
    # The roster's own number, for the roster zone.
    crowded = _model([_p(1, 10), _p(2, 8), _p(3, 6)], roster_slots=seats, season_weeks=24)
    assert crowded.benched_per_week == 6.0 and crowded.benched_season == 144.0


def test_season_weeks_fall_back_to_the_default():
    assert _model([], season_weeks=0).season_weeks == DEFAULT_SEASON_WEEKS
    assert _model([], season_weeks="x").season_weeks == DEFAULT_SEASON_WEEKS


# ---- the empty states --------------------------------------------------------


def test_no_lineup_slots_means_no_penalty():
    for slots in ({}, {"BE": 3, "IR": 1}):
        model = _model([_p(1, 10)], roster_slots=slots)
        assert model.slots == () and not model.active
        pen = model.penalty(_p(2, 10))
        assert pen.value == 0.0 and pen.reason == NO_SLOTS
        assert model.benched_per_week == 0.0


def test_a_player_with_no_team_is_left_out_and_never_penalized():
    model = _model([_p(1, 10), _p(2, 8), _p(9, 50, team=None)], roster_slots={"PG": 1, "UT": 1})
    assert model.no_team == (9,)
    assert {p.id for p in model.roster} == {1, 2}
    assert model.benched_per_week == 0.0                  # 50 is not sitting anywhere
    assert model.stacks == (Stack("DEN", 2, (1, 2)),)
    pen = model.penalty(_p(20, 30, team=None))
    assert pen.value == 0.0 and pen.reason == NO_TEAM and pen.stack == 0


def test_no_schedule_sampled_means_no_penalty():
    model = _model([_p(1, 10)], weeks=(), season_weeks=24)
    assert not model.active and model.scale == 0.0 and model.sample_weeks == ()
    assert model.benched_per_week == 0.0 and model.benched_season == 0.0
    pen = model.penalty(_p(2, 10))
    assert pen.value == 0.0 and pen.reason == NO_SCHEDULE and pen.weeks == 0


def test_stacks_count_roster_players_per_team_best_first():
    roster = [_p(1, 10), _p(2, 20), _p(3, 15), _p(4, 12, "BOS"), _p(5, 9, team=None)]
    model = _model(roster)
    assert model.stacks == (Stack("DEN", 3, (2, 3, 1)),)
    assert model.penalty(_p(7, 5, "DEN")).stack == 3
    # Largest first, then by team name; one player is not a stack.
    more = _model(roster + [_p(6, 5, "BOS"), _p(7, 5, "ATL"), _p(8, 5, "ATL")])
    assert [(s.team, s.count) for s in more.stacks] == [("DEN", 3), ("ATL", 2), ("BOS", 2)]
