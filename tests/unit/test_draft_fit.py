"""
Category fit: the weights one roster's board is scored with.

Fabricated z's throughout — the point of these tests is the *model*, not the
z-scoring underneath it (which `test_category_rank.py` owns). Every pool here
has a mean of zero per category so pace arithmetic is checkable by hand.
"""

import math

import pytest

from services.draft_fit import (
    FIT_GAIN,
    NEED_CLAMP,
    build_fit_model,
    draftable_tier_size,
    normalize_punts,
)
from services.scoring.category_value import default_category_defs
from services.scoring.models import CategoryDef

pytestmark = pytest.mark.unit

REB_AST = [CategoryDef.for_key("reb"), CategoryDef.for_key("ast")]

# Four players, mirrored: a big who rebounds and cannot pass, a guard who is
# the reverse, and two lesser copies. Mean 0 per category, so an average team's
# pace is 0 and a roster's own holdings are the whole story.
POOL: list[tuple[int, dict[str, float]]] = [
    (1, {"reb": 2.0, "ast": -2.0}),     # big
    (2, {"reb": 1.0, "ast": -1.0}),
    (3, {"reb": -1.0, "ast": 1.0}),
    (4, {"reb": -2.0, "ast": 2.0}),     # guard
]
SPREAD = math.sqrt(2.5)     # population stdev of [2, 1, -1, -2]


def _model(my_ids=(), punts=(), pool=None, categories=None, tier_size=4):
    return build_fit_model(
        pool if pool is not None else POOL,
        my_ids,
        categories if categories is not None else REB_AST,
        tier_size,
        punts,
    )


def _balanced(z):
    return sum(z.values())


# ---- the flat cases --------------------------------------------------------


def test_before_the_first_pick_fit_is_exactly_balanced():
    """An empty roster is neither behind nor ahead of anyone, so fit must not
    yet have an opinion — the two columns open the draft identical."""
    fit = _model()

    assert fit.picks_counted == 0
    assert [n.need for n in fit.needs] == [0.0, 0.0]
    assert [n.weight for n in fit.needs] == [1.0, 1.0]
    for _pid, z in POOL:
        assert fit.fit_z(_balanced(z), z) == pytest.approx(_balanced(z))
        assert fit.drivers(z) == []


def test_without_a_league_size_there_is_no_pace_to_be_behind():
    """An unsynced room knows no league to pace against; a flat model is the
    honest answer, not a pace computed from a tier of nobody."""
    fit = _model(my_ids=[4], tier_size=0)

    assert fit.tier_size == 0
    assert [n.weight for n in fit.needs] == [1.0, 1.0]
    assert fit.fit_z(_balanced(POOL[0][1]), POOL[0][1]) == pytest.approx(_balanced(POOL[0][1]))


def test_a_single_category_pool_with_no_spread_leans_nothing():
    """Every player identical in a category means there is no scale to be a
    standard deviation of — dividing by it would be the only way to fail."""
    flat = [(i, {"reb": 1.0, "ast": float(i)}) for i in range(4)]
    fit = _model(my_ids=[0], pool=flat)

    reb = next(n for n in fit.needs if n.key == "reb")
    assert reb.spread == 0.0 and reb.need == 0.0 and reb.weight == 1.0


# ---- pace and need ---------------------------------------------------------


def test_a_guard_heavy_roster_needs_boards_and_concedes_assists():
    """One guard drafted: the roster now trails an average team on the glass by
    its own holding, and leads in assists by the same. Fit leans accordingly."""
    fit = _model(my_ids=[4])                    # the guard: reb -2, ast +2

    reb = next(n for n in fit.needs if n.key == "reb")
    ast = next(n for n in fit.needs if n.key == "ast")

    assert fit.picks_counted == 1
    assert reb.mine == -2.0 and reb.pace == 0.0
    assert reb.need == pytest.approx(2.0 / SPREAD, abs=1e-3)        # ~1.265 behind
    assert ast.need == pytest.approx(-2.0 / SPREAD, abs=1e-3)       # ~1.265 ahead
    assert reb.weight == pytest.approx(1 + FIT_GAIN * reb.need, abs=1e-3)
    assert ast.weight == pytest.approx(1 + FIT_GAIN * ast.need, abs=1e-3)

    # Two players the balanced board cannot separate (both z-sum 0) are
    # separated by the roster: the big fills the hole, the guard deepens it.
    big, guard = POOL[0][1], POOL[3][1]
    assert _balanced(big) == _balanced(guard) == 0.0
    assert fit.fit_z(0.0, big) > 0 > fit.fit_z(0.0, guard)


def test_the_best_fit_is_the_big_even_when_the_best_player_is_a_guard():
    """The whole point of the column: with three guards rostered, a slightly
    worse big outranks a slightly better guard *for this team*."""
    pool = POOL + [
        (5, {"reb": 1.5, "ast": -0.5}),      # big-ish, balanced z-sum 1.0
        (6, {"reb": -0.5, "ast": 1.7}),      # guard, better balanced z-sum 1.2
    ]
    fit = _model(my_ids=[3, 4], pool=pool, tier_size=6)

    big, guard = pool[4][1], pool[5][1]
    assert _balanced(guard) > _balanced(big)                        # guard leads balanced
    assert fit.fit_z(_balanced(big), big) > fit.fit_z(_balanced(guard), guard)   # big leads fit


def test_need_is_measured_against_pace_not_against_zero():
    """Pace is what an *average team* holds after the same number of picks, so
    a roster that drafted two tier-average players is not behind at all."""
    # A pool whose top four average +1 rebound: pace after two picks is +2.
    pool = [(i, {"reb": 1.0, "ast": 0.5 * i}) for i in range(1, 5)]
    fit = build_fit_model(pool, [1, 2], REB_AST, tier_size=4)

    reb = next(n for n in fit.needs if n.key == "reb")
    assert reb.pace == 2.0 and reb.mine == 2.0 and reb.need == 0.0


def test_weights_lean_a_ranking_and_never_invert_a_category():
    """However far behind a roster falls, a good rebounding line must not
    become a bad one — the clamp is what keeps fit a lean rather than a flip."""
    # Three replacement-level bigs, drafted but below the tier, so the pace
    # they are measured against stays the tier's own.
    behind = [(9, {"reb": -3.0, "ast": 0.0}), (8, {"reb": -3.0, "ast": 0.0}),
              (7, {"reb": -3.0, "ast": 0.0})]
    fit = build_fit_model(POOL + behind, [9, 8, 7], REB_AST, tier_size=4)

    reb = next(n for n in fit.needs if n.key == "reb")
    assert reb.need == NEED_CLAMP                       # 3.29 unclamped
    assert reb.weight == 1 + FIT_GAIN * NEED_CLAMP      # 1.5, the ceiling
    assert all(0.5 <= n.weight <= 1.5 for n in fit.needs)


def test_a_drafted_player_the_pool_cannot_score_is_not_paced_against():
    """A rookie with no stat line is absent from the roster's holdings; pacing
    against him anyway would report the roster as behind in every category."""
    fit = _model(my_ids=[4, 404])           # 404 is not in the pool at all

    assert fit.picks_counted == 1           # the guard alone
    reb = next(n for n in fit.needs if n.key == "reb")
    assert reb.mine == -2.0


# ---- punts -----------------------------------------------------------------


def test_a_punt_zeroes_its_category_and_leaves_the_others_alone():
    fit = _model(punts=["ast"])

    ast = next(n for n in fit.needs if n.key == "ast")
    reb = next(n for n in fit.needs if n.key == "reb")
    assert ast.punted and ast.weight == 0.0
    assert not reb.punted and reb.weight == 1.0
    assert fit.punts == ["ast"]

    # The turnover-prone scorer stops being penalised for it; nothing else moves.
    big = POOL[0][1]
    assert fit.fit_z(_balanced(big), big) == pytest.approx(big["reb"])


def test_punting_reorders_the_board_without_removing_anyone():
    """A punt re-ranks; it never hides. Both players still score, and the one
    whose value sat in the punted category simply falls behind."""
    fit = _model(punts=["ast"])
    guard, big = POOL[3][1], POOL[0][1]

    assert _balanced(guard) == _balanced(big)
    assert fit.fit_z(_balanced(big), big) > fit.fit_z(_balanced(guard), guard)
    assert fit.fit_z(_balanced(guard), guard) == pytest.approx(guard["reb"])   # scored, not dropped


def test_drivers_name_the_categories_that_moved_the_pick():
    fit = _model(my_ids=[4], punts=["ast"])
    drivers = fit.drivers(POOL[0][1])

    assert [need.key for need, _shift in drivers] == ["ast", "reb"]
    ast_need, ast_shift = drivers[0]
    assert ast_need.punted and ast_shift == pytest.approx(2.0)   # (0 - 1) x -2
    reb_need, reb_shift = drivers[1]
    assert reb_shift == pytest.approx((reb_need.weight - 1) * 2.0)


def test_normalize_punts_dedupes_lowercases_and_reports_the_unknown():
    kept, unknown = normalize_punts(
        [" FT_PCT ", "ft_pct", "tov", "dunks", ""], ["ft_pct", "tov", "pts"]
    )
    assert kept == ["ft_pct", "tov"] and unknown == ["dunks"]

    assert normalize_punts([], ["pts"]) == ([], [])
    assert normalize_punts(["pts"], []) == ([], ["pts"])


# ---- the tier --------------------------------------------------------------


def test_the_tier_is_what_a_whole_league_drafts():
    assert draftable_tier_size(12, 13) == 156
    assert draftable_tier_size(10, None) == 130     # the default roster size
    assert draftable_tier_size(None, 13) == 0       # no league, no pace
    assert draftable_tier_size(0, 13) == 0


def test_pace_comes_from_the_draftable_tier_not_the_whole_pool():
    """Replacement-level players are not who an average team drafts, so they
    must not drag the pace down."""
    pool = POOL + [(i, {"reb": -10.0, "ast": -10.0}) for i in range(90, 100)]
    tiered = build_fit_model(pool, [4], REB_AST, tier_size=4)
    everyone = build_fit_model(pool, [4], REB_AST, tier_size=len(pool))

    reb_tiered = next(n for n in tiered.needs if n.key == "reb")
    reb_all = next(n for n in everyone.needs if n.key == "reb")
    assert reb_tiered.pace == 0.0
    assert reb_all.pace < 0        # the scrubs pull an "average team" under water


def test_a_nine_cat_league_gets_a_weight_for_every_category():
    cats = default_category_defs()
    pool = [(i, {c.key: float(i - 2) for c in cats}) for i in range(5)]
    fit = build_fit_model(pool, [0], cats, tier_size=5, punts=["tov"])

    assert [n.key for n in fit.needs] == [c.key for c in cats]
    assert all(n.label for n in fit.needs)
    tov = next(n for n in fit.needs if n.key == "tov")
    assert tov.punted and tov.weight == 0.0
    # Player 0 is the worst in every category, so the roster trails everywhere.
    assert all(n.need > 0 for n in fit.needs if n.key != "tov")


def test_an_all_ones_model_returns_the_balanced_value_untouched():
    """Not approximately: the fit column must not differ from the value column
    by a float's worth on a board where nothing has been drafted or punted."""
    fit = _model()
    for _pid, z in POOL:
        assert fit.shift(z) == 0.0
        assert fit.fit_z(12.345, z) == 12.345


def test_the_drivers_sum_to_the_shift():
    fit = _model(my_ids=[4], punts=["ast"])
    for _pid, z in POOL:
        assert sum(s for _need, s in fit.drivers(z)) == pytest.approx(fit.shift(z))
