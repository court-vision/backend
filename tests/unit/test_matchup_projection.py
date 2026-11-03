"""
services.matchup_projection: the what-if projection (today's projection with moves
applied) and the drop choice. Pure: no provider, no DB.
"""

import pytest

from services.matchup_projection import (
    DropCandidate,
    Move,
    MoveInvalid,
    SidePlayer,
    apply_moves,
    category_projection,
    choose_drop,
    games_counted,
    points_projection,
)
from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryDef, CategoryTeamScoreData, StatLine

LAST = 6     # a seven-day matchup, days 0..6


def side(pid, name, value, *days, counts=True, line=None, team="DEN"):
    return SidePlayer(pid, name, team, value, tuple(days), counts, line)


A = side(1, "A", 30.0, 0, 2, 4, 6)                      # four games left
B = side(2, "B", 20.0, 1, 3, 5)                         # three
HURT = side(3, "Hurt", 40.0, 0, 1, 2, 3, counts=False)  # on IR: games, but counts for nothing
FA = side(9, "FA", 25.0, 2, 3, 5, 6)


def counted(what_if):
    return {p.name: games for p, games in what_if.counted}


# ---- applying moves ----------------------------------------------------------------------


@pytest.mark.unit
def test_without_moves_every_player_counts_for_his_games_left():
    assert dict((p.name, g) for p, g in games_counted([A, B, HURT], last_day=LAST)) == {"A": 4, "B": 3, "Hurt": 4}


@pytest.mark.unit
def test_a_drop_stops_counting_from_its_day_and_an_add_counts_from_it():
    what_if = apply_moves([A, B], [Move(3, add=FA, drop_id=2)], last_day=LAST)
    # B keeps his day-1 game and loses days 3 and 5; FA counts from day 3: days 3, 5, 6.
    assert counted(what_if) == {"A": 4, "B": 1, "FA": 3}
    (applied,) = what_if.moves
    assert (applied.day, applied.add.name, applied.add_games, applied.drop.name, applied.drop_games) == (3, "FA", 3, "B", 2)


@pytest.mark.unit
def test_a_move_on_day_0_is_the_whole_week():
    what_if = apply_moves([A, B], [Move(0, add=FA, drop_id=1)], last_day=LAST)
    assert counted(what_if) == {"A": 0, "B": 3, "FA": 4}


@pytest.mark.unit
def test_moves_apply_in_day_order_to_one_roster_so_a_stream_chains():
    """A in for B today, then C in for A on day 4: A counts days 2 only... and C from day 4."""
    c = side(10, "C", 22.0, 4, 5, 6)
    fa = side(9, "FA", 25.0, 0, 2, 4, 6)
    what_if = apply_moves([A, B], [Move(4, add=c, drop_id=9), Move(0, add=fa, drop_id=2)], last_day=LAST)
    assert counted(what_if) == {"A": 4, "B": 0, "FA": 2, "C": 3}
    assert [m.day for m in what_if.moves] == [0, 4]


@pytest.mark.unit
def test_an_add_without_a_drop_and_a_drop_without_an_add():
    assert counted(apply_moves([A], [Move(1, add=FA)], last_day=LAST)) == {"A": 4, "FA": 4}
    assert counted(apply_moves([A, B], [Move(1, drop_id=1)], last_day=LAST)) == {"A": 1, "B": 3}


@pytest.mark.unit
def test_a_player_who_does_not_count_adds_no_games_when_added():
    out = side(11, "Out", 50.0, 1, 2, counts=False)
    what_if = apply_moves([A], [Move(0, add=out)], last_day=LAST)
    assert what_if.moves[0].add_games == 0 and points_projection(0.0, what_if.counted) == 120.0


@pytest.mark.unit
def test_games_after_the_last_day_never_count():
    late = side(12, "Late", 10.0, 5, 6, 7, 8)
    assert counted(apply_moves([late], [], last_day=LAST)) == {"Late": 2}
    assert apply_moves([A], [Move(6, add=late)], last_day=LAST).moves[0].add_games == 1


@pytest.mark.unit
def test_invalid_moves_are_refused():
    with pytest.raises(MoveInvalid, match="not on the roster"):
        apply_moves([A], [Move(0, drop_id=2)], last_day=LAST)
    with pytest.raises(MoveInvalid, match="already on the roster"):
        apply_moves([A], [Move(0, add=A)], last_day=LAST)
    with pytest.raises(MoveInvalid, match="not on the roster"):          # dropped on day 0, dropped again on day 2
        apply_moves([A, B], [Move(0, drop_id=1), Move(2, drop_id=1)], last_day=LAST)
    with pytest.raises(MoveInvalid, match="not on the roster on day 1"):   # dropped before he arrives (day 3)
        apply_moves([A], [Move(3, add=FA), Move(1, drop_id=9, add=B)], last_day=LAST)


# ---- scoring -----------------------------------------------------------------------------


@pytest.mark.unit
def test_the_points_projection_is_the_current_score_plus_value_times_games():
    before = games_counted([A, B, HURT], last_day=LAST)
    assert points_projection(100.0, before) == 100 + 30 * 4 + 20 * 3            # Hurt counts for nothing
    after = apply_moves([A, B, HURT], [Move(3, add=FA, drop_id=2)], last_day=LAST).counted
    assert points_projection(100.0, after) == 100 + 30 * 4 + 20 * 1 + 25 * 3


@pytest.mark.unit
def test_the_category_projection_overlays_lines_and_recomputes_rates():
    cats = CategoryScoring([CategoryDef.for_key("pts"), CategoryDef.for_key("fg_pct"), CategoryDef.for_key("tov")])
    base = CategoryTeamScoreData(totals={"pts": 100.0, "fg_pct": 0.5, "tov": 10.0},
                                 raw={"fgm": 50.0, "fga": 100.0, "ftm": 0.0, "fta": 0.0, "fg3m": 0.0, "fg3a": 0.0})
    a = side(1, "A", 0.0, 0, 1, line=StatLine(pts=20, fgm=10, fga=20, tov=2))
    b = side(2, "B", 0.0, 0, line=StatLine(pts=10, fgm=2, fga=10, tov=1))
    before = category_projection(cats, base, games_counted([a, b], last_day=LAST))
    assert before == {"pts": 150.0, "fg_pct": round(72 / 150, 4), "tov": 15.0}
    after = category_projection(cats, base, apply_moves([a, b], [Move(0, drop_id=2)], last_day=LAST).counted)
    assert after == {"pts": 140.0, "fg_pct": 0.5, "tov": 14.0}
    # A player without a line adds nothing.
    assert category_projection(cats, base, [(side(3, "C", 9.0, 0), 1)]) == base.totals


# ---- the drop choice ---------------------------------------------------------------------


def cand(pid, name, weighed, *days, projected=False, counts=True):
    return DropCandidate(side(pid, name, weighed or 0.0, *days, counts=counts), weighed, projected)


@pytest.mark.unit
def test_the_drop_is_the_player_whose_games_left_cost_the_least():
    choice = choose_drop([cand(1, "Star", 40.0, 0, 2, 4), cand(2, "Scrub", 8.0, 1, 3, 5), cand(3, "Idle", 20.0, 0)],
                         day=1, last_day=LAST)
    # From day 1: Star 40 x 2, Scrub 8 x 3 = 24, Idle's one game was day 0: 0.
    assert choice.candidate.player.name == "Idle" and (choice.games_lost, choice.cost) == (0, 0.0)
    choice = choose_drop([cand(1, "Star", 40.0, 0, 2, 4), cand(2, "Scrub", 8.0, 1, 3, 5)], day=0, last_day=LAST)
    assert choice.candidate.player.name == "Scrub" and choice.cost == 24.0


@pytest.mark.unit
def test_an_injured_player_keeps_his_games_and_an_unknown_is_never_dropped():
    # Hurt has four games at 40 and doesn't count today; he is still worth 160 to lose.
    choice = choose_drop([cand(3, "Hurt", 40.0, 0, 1, 2, 3, counts=False), cand(2, "B", 20.0, 1, 3, 5)], day=0, last_day=LAST)
    assert choice.candidate.player.name == "B"
    assert choose_drop([cand(4, "Unknown", None, 0, 1), cand(2, "B", 20.0, 1)], day=0, last_day=LAST).candidate.player.name == "B"
    assert choose_drop([cand(4, "Unknown", None, 0, 1)], day=0, last_day=LAST) is None
    assert choose_drop([], day=0, last_day=LAST) is None


@pytest.mark.unit
def test_ties_go_to_fewer_games_then_the_lower_value_then_the_name():
    two_at_ten = cand(1, "Z", 10.0, 0, 1)           # 20 over two games
    one_at_twenty = cand(2, "Y", 20.0, 0)           # 20 over one game
    assert choose_drop([two_at_ten, one_at_twenty], day=0, last_day=LAST).candidate.player.name == "Y"
    same = [cand(1, "Z", 10.0, 0), cand(2, "A", 10.0, 0)]
    assert choose_drop(same, day=0, last_day=LAST).candidate.player.name == "A"
    projected = choose_drop([cand(5, "P", 12.0, 0, projected=True)], day=0, last_day=LAST)
    assert projected.candidate.projected and projected.cost == 12.0
