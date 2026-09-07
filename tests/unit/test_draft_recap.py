"""The recap's arithmetic: pick pricing, seat grades, projected standings.

No database and no ESPN — every input here is a literal, which is the point of
keeping the math in a pure module.
"""

import pytest

from services.draft_recap import (
    GRADE_CURVE,
    RecapPick,
    build_recap,
    grade_for,
    positions_of,
)
from services.scoring.models import CategoryDef

pytestmark = pytest.mark.unit


CATEGORIES = [CategoryDef.for_key(k) for k in ("pts", "reb", "ast")]


def ladder_of(size: int) -> list[tuple[int, float]]:
    """Players 1..size, worth 100 down to 100 - size + 1, best first."""
    return [(pid, float(100 - pid + 1)) for pid in range(1, size + 1)]


def pick(overall: int, slot: int, player_id: int | None, **kw) -> RecapPick:
    return RecapPick(overall_pick=overall, slot=slot, player_id=player_id, **kw)


def snake(seats: int, rounds: int) -> list[tuple[int, int]]:
    """(overall_pick, seat) for a snake, so tests read like a real draft."""
    out = []
    for rnd in range(rounds):
        order = range(1, seats + 1) if rnd % 2 == 0 else range(seats, 0, -1)
        for seat in order:
            out.append((len(out) + 1, seat))
    return out


# ------------------------------ positions -------------------------------- #


def test_ties_take_the_average_of_the_positions_they_occupy():
    places = positions_of({1: 10.0, 2: 5.0, 3: 5.0, 4: 1.0})
    assert places == {1: 1.0, 2: 2.5, 3: 2.5, 4: 4.0}
    # The pot is fixed however the seats tie — this is what roto points ride on.
    assert sum(places.values()) == 4 * 5 / 2


def test_a_league_that_ties_everywhere_still_spends_the_whole_pot():
    places = positions_of({slot: 3.0 for slot in range(1, 13)})
    assert set(places.values()) == {6.5}
    assert sum(places.values()) == 12 * 13 / 2


# -------------------------------- grades --------------------------------- #


def test_grades_run_the_whole_curve_and_never_improve_as_the_seat_falls():
    grades = [grade_for(pos, 12) for pos in range(1, 13)]
    assert grades[0] == "A" and grades[-1] == "F"
    assert grades == sorted(grades)  # letters are alphabetical worst-last
    assert set(grades) == set(GRADE_CURVE)


def test_four_seats_top_out_at_d_because_an_f_is_a_claim_four_teams_cannot_carry():
    assert [grade_for(pos, 4) for pos in range(1, 5)] == ["A", "B", "C", "D"]


def test_a_league_where_every_seat_ties_gives_them_all_the_same_letter():
    assert {grade_for(6.5, 12) for _ in range(12)} == {"C"}


# ------------------------------ pick pricing ------------------------------ #


def test_a_seat_that_took_the_board_at_every_pick_is_par_and_grades_mid_field():
    """Σ value_over_slot is 0 for the seat that drafted CV's own ladder."""
    ladder = ladder_of(60)
    picks = []
    for overall, seat in snake(5, 4):
        # Seat 3 takes exactly the player CV ranked at that pick. The seats above
        # it reach for better players than the slot; the seats below settle.
        offset = {1: -2, 2: -1, 3: 0, 4: 1, 5: 2}[seat]
        picks.append(pick(overall, seat, min(60, max(1, overall + offset))))

    recap = build_recap(picks, ladder)
    par = next(seat for seat in recap.seats if seat.slot == 3)

    assert par.value_over_slot == 0.0
    assert par.position == 3.0
    assert par.grade == "C"
    assert [s.grade for s in recap.seats] == ["A", "B", "C", "D", "F"]
    assert recap.graded_by == "value_over_slot"


def test_a_pick_is_priced_against_the_player_cv_ranked_at_that_pick_number():
    ladder = ladder_of(10)  # values 100, 99, ... 91
    recap = build_recap([pick(3, 1, 1)], ladder, {1: {"overall_rank": 5, "adp": 7.5}})
    scored = recap.picks[0]

    assert scored.value == 100.0
    assert scored.cv_rank == 1
    assert scored.value_over_slot == 2.0        # 100 against the 98 at pick 3
    assert scored.surplus_cv == -2              # taken two picks ahead of his rank
    assert scored.surplus_market == 4.5         # ESPN had him going at 7.5
    assert scored.market_rank == 5


def test_a_pick_past_the_end_of_the_ladder_has_no_slot_to_be_priced_against():
    recap = build_recap([pick(50, 1, 1)], ladder_of(10))
    assert recap.picks[0].value_over_slot is None


# --------------------------- picks we cannot score ------------------------ #


def test_an_unresolved_pick_is_listed_never_dropped_and_never_charged():
    ladder = ladder_of(10)
    picks = [pick(1, 1, 1), pick(2, 1, None, espn_player_id=4242, player_name="Rookie")]

    recap = build_recap(picks, ladder)
    seat = recap.seats[0]

    assert [p.pick.overall_pick for p in recap.picks] == [1, 2]
    assert recap.picks[1].value is None and recap.picks[1].value_over_slot is None
    assert recap.unscored == 1 and seat.unscored == 1
    assert seat.picks == 2
    assert seat.value_over_slot == 0.0  # priced on pick 1 alone, not charged for pick 2


def test_a_pick_with_no_seat_is_still_reported():
    recap = build_recap([RecapPick(overall_pick=1, player_id=1)], ladder_of(5))
    assert recap.unattributed == 1
    assert recap.seats == []
    assert recap.standings == []


# -------------------------------- auction -------------------------------- #


def test_an_auction_grades_on_what_it_drafted_because_pick_order_prices_nothing():
    ladder = ladder_of(8)
    # Nomination order says nothing about who was available, so seat 4 winning
    # the best player at the last nomination is not a steal — it is a purchase.
    picks = [pick(1, 1, 3), pick(2, 2, 5), pick(3, 3, 7), pick(4, 4, 1)]

    recap = build_recap(picks, ladder, draft_type="auction")

    assert recap.graded_by == "value"
    assert all(p.value_over_slot is None for p in recap.picks)
    assert all(p.surplus_cv is not None for p in recap.picks)  # rank surplus still reads
    assert [s.total_value for s in recap.seats] == [98.0, 96.0, 94.0, 100.0]
    assert [s.grade for s in recap.seats] == ["B", "C", "D", "A"]
    assert all(s.value_over_slot is None for s in recap.seats)


# ------------------------------- standings ------------------------------- #


def category_recap(seats: int = 4, rounds: int = 3):
    ladder = ladder_of(seats * rounds)
    z = {
        pid: {"pts": 1.0 - pid / 10, "reb": pid / 10, "ast": 0.5}
        for pid, _ in ladder
    }
    picks = [pick(overall, seat, overall) for overall, seat in snake(seats, rounds)]
    return build_recap(picks, ladder, is_categories=True, categories=CATEGORIES, category_z=z)


def test_roto_points_spend_exactly_the_pot_in_every_category():
    recap = category_recap(seats=4)
    for index, cat in enumerate(CATEGORIES):
        awarded = sum(s.categories[index].roto_points for s in recap.standings)
        assert awarded == 4 * 5 / 2, cat.key


def test_a_category_every_seat_ties_splits_its_points_evenly():
    recap = category_recap(seats=4)
    ast = [s.categories[2] for s in recap.standings]  # every player is 0.5 in ast
    assert {line.rank for line in ast} == {2.5}
    assert {line.roto_points for line in ast} == {2.5}


def test_head_to_head_accounts_for_every_category_against_every_other_seat():
    recap = category_recap(seats=4)
    for standing in recap.standings:
        assert len(standing.h2h) == 3
        for cell in standing.h2h:
            assert cell.won + cell.lost + cell.tied == len(CATEGORIES)
        assert standing.expected_wins == round(
            sum(c.won + c.tied / 2 for c in standing.h2h) / 3, 2
        )


def test_roto_totals_rank_the_seats_and_carry_their_own_category_lines():
    recap = category_recap(seats=4)
    assert sum(s.roto_points for s in recap.standings) == len(CATEGORIES) * 4 * 5 / 2
    assert sorted(s.roto_rank for s in recap.standings) == sorted(
        positions_of({s.slot: s.roto_points for s in recap.standings}).values()
    )


def test_a_points_league_projects_season_value_instead_of_standings():
    ladder = ladder_of(4)
    picks = [pick(1, 1, 1), pick(2, 2, 2), pick(3, 2, 3), pick(4, 1, 4)]
    season = {1: 6500.0, 2: 6400.0, 3: 6300.0, 4: 6200.0}

    recap = build_recap(picks, ladder, season_value=season)

    assert [s.season_value for s in recap.standings] == [12700.0, 12700.0]
    assert [s.value_rank for s in recap.standings] == [1.5, 1.5]
    assert all(s.categories == [] and s.roto_points is None for s in recap.standings)
