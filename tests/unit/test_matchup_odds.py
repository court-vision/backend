"""
The win chance guessed from a matchup's own numbers (services/matchup_odds.py): the Home desk's
colour and order for the matchups the league projection has no chance for.
"""

import pytest

from services import matchup_odds as odds
from services.matchup_odds import CategoryRace

pytestmark = pytest.mark.unit


def race(key, now, final, *, higher=True, rate=False, attempts=(None, None)):
    return CategoryRace(key=key, higher_is_better=higher, is_rate=rate, now=now, final=final, attempts=attempts)


def ends(*wins):
    """Races with no chance of a tie, by their chance of a win."""
    return [(p, 0.0) for p in wins]


WON, LOST, LEVEL = (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)


# ------------------------------- points ------------------------------- #


def test_a_points_week_level_on_paper_is_a_coin_flip_and_a_projected_lead_tips_it():
    assert odds.points_chance(0, 0, 1300, 1300) == 0.5
    small, big = odds.points_chance(0, 0, 1350, 1300), odds.points_chance(0, 0, 1400, 1250)
    assert 0.5 < small < big < 0.9                                     # a whole week to play: nothing is settled
    assert odds.points_chance(0, 0, 1300, 1350) == pytest.approx(1 - small)


def test_the_same_lead_is_worth_more_with_less_left_to_play():
    early = odds.points_chance(60, 0, 1360, 1300)
    late = odds.points_chance(1260, 1200, 1360, 1300)
    assert 0.5 < early < late < 1.0
    # the score so far is fixed: only what's left spreads
    assert odds.points_chance(1300, 1240, 1300, 1240) == 1.0
    assert odds.points_chance(1240, 1300, 1240, 1300) == 0.0
    assert odds.points_chance(1300, 1300, 1300, 1300) == 0.5


def test_a_score_past_its_projection_stands_and_no_numbers_is_no_guess():
    # a hot night: the score is already over the projected final, which doesn't pull it back
    assert odds.points_chance(1310, 1200, 1300, 1280) > 0.5
    assert odds.points_chance(0, 0, 0, 0) is None


# ------------------------------- one category ------------------------------- #


def test_a_counting_category_is_decided_by_its_lead_against_what_is_left():
    whole = (1.0, 1.0)
    assert odds.category_chance(race("pts", (0, 0), (620, 620)), whole) == pytest.approx(0.5)
    ahead = odds.category_chance(race("pts", (0, 0), (640, 600)), whole)
    assert 0.5 < ahead < 0.9
    # the same 40 with 40 left a side is nearly over
    assert odds.category_chance(race("pts", (600, 560), (640, 600)), whole) > 0.95
    # nothing left: the totals are the result
    assert odds.category_chance(race("stl", (40, 38), (40, 38)), whole) == 1.0
    assert odds.category_chance(race("stl", (38, 38), (38, 38)), whole) == 0.5


def test_a_steal_lead_is_safer_than_the_same_share_of_a_points_lead():
    whole = (1.0, 1.0)
    # 10% up in both: steals come one at a time, points in lumps, so over a full week the bigger total
    # is the surer one, and with the same amount left the steadier category is
    assert odds.category_chance(race("stl", (0, 0), (44, 40)), whole) < odds.category_chance(race("pts", (0, 0), (660, 600)), whole)
    assert odds.category_chance(race("stl", (0, 0), (60, 50)), whole) > odds.category_chance(race("pts", (0, 0), (60, 50)), whole)


def test_fewer_is_better_turns_a_category_around():
    whole = (1.0, 1.0)
    fewer = odds.category_chance(race("tov", (0, 0), (70, 85), higher=False), whole)
    assert fewer > 0.5 and fewer == pytest.approx(1 - odds.category_chance(race("tov", (0, 0), (85, 70), higher=False), whole))


def test_a_rate_firms_up_as_its_attempts_are_taken():
    fg = lambda attempts=(None, None): race("fg_pct", (0.48, 0.46), (0.48, 0.46), rate=True, attempts=attempts)   # noqa: E731
    start = odds.category_chance(fg(), (1.0, 1.0))                     # a week of typical attempts to come
    late = odds.category_chance(fg((360.0, 350.0)), (0.1, 0.1))        # nine tenths of them taken
    assert 0.5 < start < late < 1.0
    assert odds.category_chance(fg((400.0, 400.0)), (0.0, 0.0)) == 1.0  # none left: the rate is the result
    # a rate that is no proportion of attempts still gets a spread
    ato = race("ato", (2.0, 1.8), (2.0, 1.8), rate=True)
    assert 0.5 < odds.category_chance(ato, (1.0, 1.0)) < odds.category_chance(ato, (0.1, 0.1)) < 1.0


def test_the_share_left_is_the_middle_of_the_counting_categories():
    races = [race("pts", (300, 0), (600, 500)), race("reb", (150, 0), (200, 200)), race("ast", (90, 0), (100, 0)),
             race("fg_pct", (0.5, 0.5), (0.5, 0.5), rate=True)]
    assert odds.share_left(races, 0) == 0.25                           # 0.5, 0.25, 0.1 left: the middle
    assert odds.share_left(races, 1) == 1.0                            # a category projected at nothing says nothing
    assert odds.share_left([races[3]], 0) == 0.5                       # no counting category: half


# ------------------------------- the week ------------------------------- #


def test_a_counting_category_can_end_level_and_a_rate_cannot():
    whole = (1.0, 1.0)
    # finished and level: a tie for certain, not a coin flip
    assert odds.category_outcome(race("stl", (38, 38), (38, 38)), whole) == LEVEL
    assert odds.category_outcome(race("stl", (40, 38), (40, 38)), whole) == WON
    assert odds.category_outcome(race("tov", (40, 38), (40, 38), higher=False), whole) == LOST
    # still to play: whole numbers can meet, and the fewer there are to come the likelier it is
    win, tie = odds.category_outcome(race("blk", (0, 0), (25, 25)), whole)
    late_win, late_tie = odds.category_outcome(race("blk", (23, 23), (25, 25)), whole)
    assert 0 < tie < late_tie < 0.5
    assert win == pytest.approx(1 - win - tie) and late_win == pytest.approx(1 - late_win - late_tie)
    # a rate is not a whole number: level on paper is a coin flip, and only a finished one ties
    assert odds.category_outcome(race("fg_pct", (0.47, 0.47), (0.47, 0.47), rate=True), whole) == (0.5, 0.0)
    assert odds.category_outcome(race("fg_pct", (0.47, 0.47), (0.47, 0.47), rate=True), (0.0, 0.0)) == LEVEL


def test_the_week_goes_to_whoever_wins_more_categories_than_they_lose_and_a_level_week_counts_half():
    assert odds.week_chance([]) is None
    assert odds.week_chance(ends(1.0, 1.0, 0.0)) == 1.0
    assert odds.week_chance(ends(1.0, 0.0, 0.0)) == 0.0
    assert odds.week_chance(ends(1.0, 1.0, 0.0, 0.0)) == 0.5           # 2 to 2: level
    assert odds.week_chance(ends(*[0.5] * 9)) == pytest.approx(0.5)
    assert odds.week_chance(ends(*[0.5] * 8)) == pytest.approx(0.5)
    # four safe and one coin flip in nine: the other four must all go wrong
    assert odds.week_chance(ends(*[1.0] * 4, 0.5, *[0.0] * 4)) == pytest.approx(0.5)
    assert odds.week_chance(ends(*[1.0] * 5, *[0.0] * 4)) == 1.0


def test_a_category_that_ends_level_counts_for_neither_side():
    # a finished week of 4 won, 3 lost and 2 level is won, as ESPN counts it: not a coin flip on the two
    assert odds.week_chance([WON] * 4 + [LOST] * 3 + [LEVEL] * 2) == 1.0
    assert odds.week_chance([WON] * 3 + [LOST] * 4 + [LEVEL] * 2) == 0.0
    assert odds.week_chance([WON] * 3 + [LOST] * 3 + [LEVEL] * 3) == 0.5
    assert odds.week_chance([LEVEL]) == 0.5
    # one race still open beside a level one decides the week
    assert odds.week_chance([LEVEL, (0.7, 0.1)]) == pytest.approx(0.7 + 0.1 / 2)
    finished = [race("pts", (620, 600), (620, 600)), race("reb", (240, 250), (240, 250)), race("stl", (38, 38), (38, 38))]
    assert odds.categories_chance(finished) == 0.5                     # 1 to 1 with one level
    assert odds.categories_chance(finished + [race("ast", (150, 140), (150, 140))]) == 1.0


def test_a_categories_week_reads_its_races_together():
    start = [race("pts", (0, 0), (620, 600)), race("reb", (0, 0), (240, 250)), race("ast", (0, 0), (150, 140)),
             race("stl", (0, 0), (40, 38)), race("blk", (0, 0), (25, 30)), race("fg3m", (0, 0), (70, 60)),
             race("tov", (0, 0), (80, 85), higher=False), race("fg_pct", (0, 0), (0.47, 0.465), rate=True),
             race("ft_pct", (0, 0), (0.80, 0.78), rate=True)]
    early = odds.categories_chance(start)
    assert 0.5 < early < 0.8                                           # projected 7 to 2, with the whole week to play
    late = [race(r.key, (r.final[0] * 0.93, r.final[1] * 0.93) if not r.is_rate else r.final, r.final,
                 higher=r.higher_is_better, rate=r.is_rate, attempts=(390.0, 390.0) if r.is_rate else (None, None))
            for r in start]
    assert early < odds.categories_chance(late) <= 1.0                 # the same leads with a day left
    assert odds.categories_chance([]) is None


# ------------------------------- how much is in play ------------------------------- #


def test_closeness_peaks_at_a_coin_flip_and_a_settled_week_is_decided():
    assert odds.closeness(0.5) == 1.0 and odds.closeness(0.0) == odds.closeness(1.0) == 0.0
    assert odds.closeness(0.75) == odds.closeness(0.25) == 0.5
    assert odds.closeness(None) == 0.5                                 # unknown: half in play
    assert odds.decided(0.98) and odds.decided(0.02) and odds.decided(1.0)
    assert not odds.decided(0.9) and not odds.decided(0.5) and not odds.decided(None)
