"""
DraftBoardService with the fetch layer stubbed (no DB): projection-over-baseline
composition, league-scored values for both formats, market join and delta,
stable ranks under picks, market-only rookie rows, both position-cap rules
(exact ESPN primary position, coarse fallback), and the recommendation
components.
"""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from services.draft_board_service import (
    BoardInputs,
    BoardSession,
    DraftBoardService,
    MarketOnlyRow,
)
from services.scoring.category_value import category_value
from services.scoring.models import StatLine
from services.scoring.category_rank import PoolRow
from services.scoring.resolver import resolve_scoring

SEASON = "2026-27"

NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in ("fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov")
]


def _league(**overrides):
    base = dict(
        id=3, provider="espn", provider_league_id="993431466", season=2027, name="Dunk Dynasty",
        scoring_type="points", category_win_mode=None, categories=[], point_weights={"pts": 1.0},
        matchup_periods={},
        roster_slots={"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1},
        position_limits={}, draft_settings={"pick_order": [1, 2, 3, 4]},
        raw_settings={}, settings_synced_at=datetime(2026, 8, 24, 12, 0, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _row(id: int, fpts: float, gp: int = 60, name=None, team="DEN", espn_id=None, **stats) -> PoolRow:
    return PoolRow(id=id, name=name or f"P{id}", team=team, gp=gp,
                   line=StatLine.from_dict(stats), fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
                   espn_id=espn_id, name_normalized=(name or f"P{id}").lower())


def _inputs(**overrides) -> BoardInputs:
    """Six players: 1 C star (projection over baseline), 2 G (baseline only),
    3 F (projection), 4 F-C rookie (projection only), 5 unknown-position
    (baseline, no market), 6 pure C (baseline)."""
    base = BoardInputs(
        season=SEASON,
        pool=[
            _row(1, fpts=55.0, pts=30, espn_id=101),
            _row(2, fpts=40.0, pts=22),
            _row(3, fpts=50.0, pts=28),
            _row(4, fpts=35.0, gp=0, team=None, pts=20),
            _row(5, fpts=9.0, team=None, pts=5),
            _row(6, fpts=20.0, pts=10),
        ],
        source={1: "projection", 2: "baseline", 3: "projection",
                4: "projection", 5: "baseline", 6: "baseline"},
        last_season_gp={1: 70, 2: 60, 3: 65, 5: 41, 6: 55},
        projected_gp={1: 78, 3: 74, 4: 70},
        projections_as_of=date(2026, 9, 2),
        market={
            1: {"overall_rank": 1, "adp": 1.4, "auction_value": 62.0},
            2: {"overall_rank": 6, "adp": 5.5, "auction_value": None},
            3: {"overall_rank": 2, "adp": 2.9, "auction_value": 48.0},
            4: {"overall_rank": 2, "adp": None, "auction_value": None},
        },
        market_as_of=date(2026, 9, 1),
        positions={1: "C", 2: "G", 3: "F", 4: "F-C", 5: None, 6: "C"},
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


@pytest.fixture(autouse=True)
def direct_db_boundary(monkeypatch):
    from db import base as db_base

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_base, "run_db", direct_run_db)


@pytest.fixture
def stub_inputs(monkeypatch):
    calls = []

    def fake_fetch(my_ids, session_id=None):
        calls.append((my_ids, session_id))
        return _inputs()

    monkeypatch.setattr(DraftBoardService, "_fetch_inputs", staticmethod(fake_fetch))
    return calls


def _board(scoring, picked=(), mine=(), session=None):
    return asyncio.run(
        DraftBoardService.get_board(scoring, picked_ids=picked, my_ids=mine, session=session)
    )


@pytest.mark.unit
def test_points_league_values_ranks_and_market_join(stub_inputs):
    resp = _board(resolve_scoring(_league(point_weights={"pts": 1.0})))

    assert resp.status == ApiStatus.SUCCESS
    by_id = {r.player_id: r for r in resp.data}
    # Value is the league formula over the per-game line ({"pts": 1.0} -> pts),
    # projection line where one exists, baseline otherwise.
    assert [r.player_id for r in resp.data] == [1, 3, 2, 4, 6, 5]
    assert [r.cv_rank for r in resp.data] == [1, 2, 3, 4, 5, 6]
    assert by_id[1].value == 30.0 and by_id[1].value_source == "projection"
    assert by_id[2].value == 22.0 and by_id[2].value_source == "baseline"
    # Market join and the rank-scale delta: positive = market ranks him worse than CV.
    assert by_id[2].market_rank == 6 and by_id[2].market_delta == 3
    assert by_id[4].market_rank == 2 and by_id[4].market_delta == -2
    assert by_id[5].market_rank is None and by_id[5].market_delta is None and by_id[5].adp is None
    assert by_id[1].adp == 1.4 and by_id[1].auction_value == 62.0
    # Points leagues carry no category fields.
    assert by_id[1].categories is None and by_id[1].category_z is None and by_id[1].score is None

    meta = resp.meta
    assert meta.season == SEASON and meta.format == "points" and meta.value_kind == "fpts"
    assert meta.pool_size == 6 and meta.available == 6
    assert meta.projection_count == 3 and meta.baseline_count == 3
    assert meta.projections_as_of == date(2026, 9, 2) and meta.market_as_of == date(2026, 9, 1)
    assert meta.settings_synced is True and meta.categories == []
    assert meta.unsupported == []


@pytest.mark.unit
def test_game_only_weights_are_disclosed_not_silently_dropped(stub_inputs):
    """dd/td bonuses score 0 against aggregate lines; the meta must say so
    rather than imply the league's weights were fully applied."""
    resp = _board(resolve_scoring(_league(point_weights={"pts": 1.0, "dd": 3.0, "td": 5.0})))

    assert resp.meta.unsupported == ["dd", "td"]
    # The honored part of the formula still scores: values are the pts line alone.
    by_id = {r.player_id: r for r in resp.data}
    assert by_id[1].value == 30.0 and by_id[2].value == 22.0


@pytest.mark.unit
def test_rookie_rides_the_projection_with_no_baseline(stub_inputs):
    resp = _board(resolve_scoring(_league()))

    rookie = next(r for r in resp.data if r.player_id == 4)
    assert rookie.value_source == "projection"
    assert rookie.last_season_gp is None and rookie.projected_gp == 70
    assert rookie.team is None


@pytest.mark.unit
def test_picked_players_leave_the_board_but_ranks_stay_stable(stub_inputs):
    resp = _board(resolve_scoring(_league()), picked=[3], mine=[1])

    # `mine` need not be repeated in `picked`; both sets are removed.
    assert [r.player_id for r in resp.data] == [2, 4, 6, 5]
    # cv_rank is the full-pool rank, not renumbered after removal.
    assert [r.cv_rank for r in resp.data] == [3, 4, 5, 6]
    assert resp.meta.pool_size == 6 and resp.meta.available == 4
    assert stub_inputs == [(frozenset({1}), None)]   # the fetch was told whose roster to look up


@pytest.mark.unit
def test_category_league_scores_and_maps_to_the_fpts_scale(stub_inputs):
    scoring = resolve_scoring(_league(scoring_type="categories", categories=NINE_CAT,
                                      category_win_mode="each_category"))
    resp = _board(scoring)

    assert resp.meta.format == "categories" and resp.meta.value_kind == "cat_value"
    assert [c.key for c in resp.meta.categories] == [c["key"] for c in NINE_CAT]
    scores = [r.score for r in resp.data]
    assert scores == sorted(scores, reverse=True)
    for r in resp.data:
        assert set(r.category_z) == {c["key"] for c in NINE_CAT}
        assert r.value == category_value(r.score)
        assert r.score == pytest.approx(sum(r.category_z.values()), abs=2e-3)


@pytest.mark.unit
def test_center_cap_blocks_pure_centers_only(stub_inputs):
    scoring = resolve_scoring(_league(position_limits={"C": 1}))
    resp = _board(scoring, mine=[1])            # my one roster spot: player 1, a C

    by_id = {r.player_id: r for r in resp.data}
    assert by_id[6].cap_blocked is True         # another pure C: no room left
    assert by_id[4].cap_blocked is False        # F-C counts by primary position (F)
    assert by_id[2].cap_blocked is False
    assert resp.meta.position_limits == {"C": 1}


@pytest.mark.unit
def test_a_split_cap_with_an_uncapped_sibling_blocks_nobody(stub_inputs):
    # Only PG is capped; any G we cannot tell apart could be the uncapped SG.
    resp = _board(resolve_scoring(_league(position_limits={"PG": 1})), mine=[2])
    assert not any(r.cap_blocked for r in resp.data)


@pytest.mark.unit
def test_enforceable_caps_and_primary_groups():
    caps = DraftBoardService._enforceable_caps({"PG": 2, "SG": 2, "SF": 3, "C": 0})
    assert caps == {"G": 4, "C": 0}             # F needs both SF and PF; explicit 0 is a real rule
    assert DraftBoardService._primary_group("F-C") == "F"
    assert DraftBoardService._primary_group("c") == "C"
    assert DraftBoardService._primary_group(None) is None
    assert DraftBoardService._primary_group("PF") is None   # nba_api never writes fantasy positions


@pytest.mark.unit
def test_zero_cap_blocks_even_an_empty_roster(stub_inputs):
    resp = _board(resolve_scoring(_league(position_limits={"C": 0})))
    by_id = {r.player_id: r for r in resp.data}
    assert by_id[1].cap_blocked is True and by_id[6].cap_blocked is True
    assert by_id[3].cap_blocked is False


@pytest.mark.unit
def test_empty_pool_is_a_success_envelope(monkeypatch):
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: BoardInputs(season=SEASON, pool=[])))
    resp = _board(resolve_scoring(_league()))

    assert resp.status == ApiStatus.SUCCESS and resp.data == []
    assert SEASON in resp.message
    assert resp.meta.pool_size == 0 and resp.meta.available == 0


# ---- market position data (WS1) and the exact cap rule ----------------------


def _espn_market(**by_id):
    """Market rows carrying the position fields the pipeline now captures."""
    base = _inputs().market
    for pid, extra in by_id.items():
        row = dict(base.get(int(pid), {"overall_rank": None, "adp": None, "auction_value": None}))
        row.update(extra)
        base[int(pid)] = row
    return base


@pytest.mark.unit
def test_rows_carry_espn_primary_position_eligibility_and_injury(monkeypatch):
    market = _espn_market(
        **{"1": {"default_position_id": 5, "eligible_slot_ids": [4, 9, 10, 11, 12, 13],
                 "injury_status": "DAY_TO_DAY"},
           "2": {"default_position_id": 1, "eligible_slot_ids": [0, 5, 8, 11, 12],
                 "injury_status": "ACTIVE"}}
    )
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league()))

    by_id = {r.player_id: r for r in resp.data}
    # 1-based defaultPositionId (5 = C) and 0-based slot ids, each read in its
    # own space: bench (12) and IR (13) are dropped, UT (11) is kept.
    assert by_id[1].primary_position == "C"
    assert by_id[1].positions == ["C", "PF/C", "F/C", "UT"]
    assert by_id[1].injury_status == "DAY_TO_DAY"
    assert by_id[2].primary_position == "PG"
    assert by_id[2].positions == ["PG", "G", "G/F", "UT"]
    # ACTIVE is not an injury; it reads as no flag at all.
    assert by_id[2].injury_status is None
    # A player the market snapshot does not carry keeps nulls, not guesses.
    assert by_id[6].primary_position is None and by_id[6].positions is None
    assert resp.meta.position_source == "espn"


@pytest.mark.unit
def test_exact_caps_count_espn_primary_position_not_the_coarse_group(monkeypatch):
    """ESPN counts caps by defaultPositionId, so a PF-primary player does not
    consume a centre slot even though both are coarse 'F'/'C' neighbours."""
    market = _espn_market(
        **{"1": {"default_position_id": 5},    # C
           "3": {"default_position_id": 4},    # PF
           "4": {"default_position_id": 4},    # PF (nba position 'F-C')
           "6": {"default_position_id": 5}}    # C
    )
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league(position_limits={"C": 1})), mine=[1])

    by_id = {r.player_id: r for r in resp.data}
    assert by_id[6].cap_blocked is True      # the second centre has no room
    assert by_id[3].cap_blocked is False     # a PF is not a centre under ESPN's rule
    assert by_id[4].cap_blocked is False
    # A split cap the coarse rule could never enforce is exact here.
    resp = _board(resolve_scoring(_league(position_limits={"PF": 1})), mine=[3])
    by_id = {r.player_id: r for r in resp.data}
    assert by_id[4].cap_blocked is True and by_id[6].cap_blocked is False


@pytest.mark.unit
def test_an_unknown_roster_position_falls_back_to_the_coarse_rule(monkeypatch):
    """One rostered player without ESPN data makes the exact counts wrong, so
    that candidate is judged by the conservative coarse rule instead."""
    market = _espn_market(**{"6": {"default_position_id": 5}})   # only player 6 is known
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league(position_limits={"C": 1})), mine=[1])

    by_id = {r.player_id: r for r in resp.data}
    # Player 1 (rostered) has no ESPN primary, so player 6 is counted coarsely —
    # and coarsely, player 1 is a C, so the one centre slot is gone.
    assert by_id[6].cap_blocked is True


# ---- market-only rows ------------------------------------------------------


@pytest.mark.unit
def test_a_ranked_rookie_appears_as_a_market_only_row(monkeypatch):
    market = _espn_market(**{"9": {"overall_rank": 25, "adp": 31.2, "auction_value": None,
                                   "default_position_id": 2}})
    inputs = _inputs(
        market=market,
        market_only=[MarketOnlyRow(id=9, name="Rookie Nine", espn_id=909, position="G")],
    )
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: inputs))
    resp = _board(resolve_scoring(_league()))

    rookie = next(r for r in resp.data if r.player_id == 9)
    assert rookie.value is None and rookie.value_source == "market"
    assert rookie.cv_rank is None and rookie.fpts_avg is None and rookie.market_delta is None
    assert rookie.market_rank == 25 and rookie.adp == 31.2
    assert rookie.primary_position == "SG"
    # Market-only rows sit after everything that could be valued.
    assert [r.player_id for r in resp.data][-1] == 9
    assert resp.meta.market_only_count == 1 and resp.meta.available == 7
    # ...and are never recommended: there is no value to recommend on.
    assert 9 not in [rec.player_id for rec in resp.recommendations]


@pytest.mark.unit
def test_a_drafted_market_only_row_leaves_the_board(monkeypatch):
    inputs = _inputs(market_only=[MarketOnlyRow(id=9, name="Rookie Nine")])
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: inputs))
    resp = _board(resolve_scoring(_league()), picked=[9])
    assert 9 not in [r.player_id for r in resp.data]


# ---- session picks ---------------------------------------------------------


@pytest.mark.unit
def test_session_picks_are_unioned_with_the_query_params(monkeypatch):
    inputs = _inputs(session_picked=frozenset({3}), session_mine=frozenset({1}))
    seen = []

    def fake_fetch(my_ids, session_id=None):
        seen.append((my_ids, session_id))
        return inputs

    monkeypatch.setattr(DraftBoardService, "_fetch_inputs", staticmethod(fake_fetch))
    # Both sources at once: the session holds 3 (anyone) and 1 (mine), the query
    # string adds 2 (anyone) and 6 (mine). All four must leave the board.
    resp = _board(resolve_scoring(_league(position_limits={"C": 1})), picked=[2], mine=[6],
                  session=BoardSession(session_id=77, my_slot=3, rounds=13, league_size=4))

    assert seen == [(frozenset({6}), 77)]        # the fetch was told whose roster and which session
    assert [r.player_id for r in resp.data] == [4, 5]
    assert resp.meta.session_id == 77 and resp.meta.league_size == 4


# ---- recommendations -------------------------------------------------------


@pytest.mark.unit
def test_a_centre_drafted_inside_the_session_spends_the_cap(monkeypatch):
    inputs = _inputs(session_picked=frozenset({1}), session_mine=frozenset({1}))
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: inputs))
    resp = _board(resolve_scoring(_league(position_limits={"C": 1})),
                  session=BoardSession(session_id=77))

    assert next(r for r in resp.data if r.player_id == 6).cap_blocked is True


@pytest.mark.unit
def test_every_component_is_exercised_and_the_summed_ones_equal_the_score(monkeypatch):
    """The default fixture has no ESPN positions, so scarcity and flexibility
    are structurally zero there — assert the sum where all four terms bite."""
    # Four centres so the position has a replacement level below its best
    # player (with only one left, VORP is 0 and scarcity has nothing to scale).
    market = _espn_market(
        **{"1": {"default_position_id": 5, "eligible_slot_ids": [4, 9, 11, 12]},
           "2": {"default_position_id": 1, "eligible_slot_ids": [0, 1, 5, 11, 12]},
           "3": {"default_position_id": 5, "eligible_slot_ids": [4, 11, 12],
                 "injury_status": "DOUBTFUL"},
           "4": {"default_position_id": 5, "eligible_slot_ids": [4, 11, 12]},
           "6": {"default_position_id": 5, "eligible_slot_ids": [4, 11, 12]}}
    )
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league()), picked=[6])

    seen = {"scarcity": False, "flexibility": False, "injury": False}
    for rec in resp.recommendations:
        summed = round(sum(c.value for c in rec.components if c.in_score), 1)
        assert summed == rec.score, f"{rec.name}: components do not sum to the score"
        for key in seen:
            if next(c for c in rec.components if c.key == key).value:
                seen[key] = True
    assert all(seen.values()), f"never exercised: {[k for k, v in seen.items() if not v]}"


@pytest.mark.unit
def test_recommendations_decompose_the_score_and_sum_to_it(stub_inputs):
    resp = _board(resolve_scoring(_league()))

    assert resp.recommendations, "a valued pool must produce recommendations"
    best = resp.recommendations[0]
    assert best.player_id == 1                      # the most valuable available player
    keys = [c.key for c in best.components]
    assert keys == ["season_value", "vorp", "scarcity", "flexibility", "injury"]
    summed = round(sum(c.value for c in best.components if c.in_score), 1)
    assert summed == best.score
    # season_value is context, not a term: it is the base vorp is measured from.
    assert next(c for c in best.components if c.key == "season_value").in_score is False
    assert best.season_value == round(best.value * 78, 1)     # player 1's projected gp
    assert [r.score for r in resp.recommendations] == sorted(
        (r.score for r in resp.recommendations), reverse=True
    )
    assert best.name in best.reason


@pytest.mark.unit
def test_a_player_with_no_projection_uses_the_default_games(stub_inputs):
    from services.draft_board_service import DEFAULT_PROJECTED_GP

    resp = _board(resolve_scoring(_league()))
    rec = next(r for r in resp.recommendations if r.player_id == 2)   # baseline, no projected_gp
    assert rec.season_value == round(rec.value * DEFAULT_PROJECTED_GP, 1)


@pytest.mark.unit
def test_cap_blocked_and_drafted_players_are_never_recommended(monkeypatch):
    market = _espn_market(**{"1": {"default_position_id": 5}, "6": {"default_position_id": 5}})
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league(position_limits={"C": 0})), picked=[3])

    ids = [r.player_id for r in resp.recommendations]
    assert 1 not in ids and 6 not in ids     # capped out
    assert 3 not in ids                      # already drafted
    assert next(r for r in resp.data if r.player_id == 1).cap_blocked is True


@pytest.mark.unit
def test_flexibility_rewards_extra_startable_slots_and_injury_discounts(monkeypatch):
    market = _espn_market(
        **{"2": {"default_position_id": 1, "eligible_slot_ids": [0, 1, 5, 11, 12]},   # PG/SG/G
           "6": {"default_position_id": 5, "eligible_slot_ids": [4, 11, 12],           # C only
                 "injury_status": "OUT"}}
    )
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    resp = _board(resolve_scoring(_league()))

    recs = {r.player_id: r for r in resp.recommendations}
    flex = next(c for c in recs[2].components if c.key == "flexibility")
    # PG, SG and G are all real slots in this league: two beyond the first.
    assert flex.value == round(0.02 * 2 * recs[2].season_value, 1) and flex.value > 0
    if 6 in recs:
        injury = next(c for c in recs[6].components if c.key == "injury")
        assert injury.value < 0 and "OUT" in (injury.detail or "")


@pytest.mark.unit
def test_scarcity_counts_down_the_startable_tier_not_the_whole_pool(monkeypatch):
    """The tier is fixed against the full pool, so drafting from it raises
    pressure — a tier recomputed over survivors would never run dry."""
    market = _espn_market(**{str(pid): {"default_position_id": 5} for pid in (1, 3, 4, 6)})
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: _inputs(market=market)))
    league = _league()      # 4-team pick order, 1.6 C starters -> a tier of ~6

    quiet = _board(resolve_scoring(league))
    drained = _board(resolve_scoring(league), picked=[1, 3])

    def scarcity(resp, pid):
        rec = next(r for r in resp.recommendations if r.player_id == pid)
        return next(c for c in rec.components if c.key == "scarcity")

    # Player 4 is a centre above replacement in both boards, so only the tier
    # pressure moves: two of six startable centres are gone in `drained`.
    assert scarcity(drained, 4).value > scarcity(quiet, 4).value > 0
    assert "of 6" in scarcity(drained, 4).detail
    # The bottom centre sits at replacement, so scarcity has nothing to scale.
    assert scarcity(quiet, 6).value == 0.0


@pytest.mark.unit
def test_recommendations_are_empty_when_nothing_can_be_valued(monkeypatch):
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: BoardInputs(season=SEASON, pool=[])))
    resp = _board(resolve_scoring(_league()))
    assert resp.recommendations == []


@pytest.mark.unit
def test_a_category_league_recommends_on_the_category_value(stub_inputs):
    scoring = resolve_scoring(_league(scoring_type="categories", categories=NINE_CAT,
                                      category_win_mode="each_category"))
    resp = _board(scoring)

    assert resp.recommendations
    best = resp.recommendations[0]
    row = next(r for r in resp.data if r.player_id == best.player_id)
    assert best.value == row.value           # the same fpts-scale category value
    assert [c.key for c in best.components][1] == "vorp"


@pytest.mark.unit
def test_starters_per_position_shares_derived_slots():
    starters = DraftBoardService._starters_per_position(
        {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1}
    )
    # A G slot is half a PG and half an SG; a UT slot is a fifth of each.
    assert starters["PG"] == pytest.approx(1 + 0.5 + 0.6)
    assert starters["C"] == pytest.approx(1 + 0.6)
    # Every started slot is counted exactly once: 5 + G + F + 3 UT = 10 starters.
    assert sum(starters.values()) == pytest.approx(10.0)


@pytest.mark.unit
def test_the_replacement_bar_does_not_fall_as_a_position_is_drafted():
    """Indexing the *original* starter need into a shrinking available list
    would slide the bar deeper into the distribution and hand a picked-over
    position a growing VORP premium — the opposite of what scarcity means."""
    def candidates(drafted_top: int) -> list[dict]:
        pool = [
            {"id": 100 + i, "name": f"C{i}", "value": 45 - i * 0.5,
             "season_value": round((45 - i * 0.5) * 65, 1), "position": "C",
             "available": i >= drafted_top, "blocked": False, "injury": None, "slots": ["C", "UT"]}
            for i in range(60)
        ]
        pool.append({"id": 999, "name": "Star", "value": 45.0, "season_value": 2925.0,
                     "position": "PG", "available": True, "blocked": False,
                     "injury": None, "slots": ["PG", "G", "UT"]})
        return pool

    league = _league(draft_settings={"pick_order": list(range(1, 11))})
    scoring = resolve_scoring(league)

    def best_centre(drafted):
        recs = DraftBoardService._recommend(candidates(drafted), scoring,
                                            BoardSession(league_size=10), frozenset(), {})
        return next(r for r in recs if r.primary_position == "C"), recs

    fresh, _ = best_centre(0)
    mid, _ = best_centre(10)
    picked_clean, recs = best_centre(20)

    # The bar tracks the same replacement-level player while the tier drains...
    assert mid.season_value - mid.vorp == pytest.approx(fresh.season_value - fresh.vorp)
    # ...and once every team's centre slot is filled, the next centre is worth
    # no more than the best one still sitting there.
    assert picked_clean.vorp == 0.0
    # So an untouched star outranks the survivors of a run, rather than losing
    # to them on an inflated VORP.
    assert recs[0].name == "Star"


# ---- the fetch layer itself ------------------------------------------------


class _Query(list):
    """A stand-in for a peewee query: iterable, and `.where()` returns itself."""

    def where(self, *_args, **_kwargs):
        return self


@pytest.fixture
def fake_tables(monkeypatch):
    """Replace every table `_fetch_inputs` reads with an in-memory stand-in."""
    from services import draft_board_service as module

    players = {
        1: SimpleNamespace(id=1, name="Star", position="C", espn_id=101, name_normalized="star"),
        2: SimpleNamespace(id=2, name="Guard", position="G", espn_id=102, name_normalized="guard"),
        9: SimpleNamespace(id=9, name="Rookie", position="F", espn_id=109, name_normalized="rookie"),
    }
    state = {"players": players, "picks": [], "projections": [], "market": []}

    monkeypatch.setattr(module.settings, "nba_season", SEASON, raising=False)
    monkeypatch.setattr(module, "load_baseline_pool",
                        lambda: [_row(1, fpts=50.0, gp=70, name="Star", espn_id=101),
                                 _row(2, fpts=30.0, gp=60, name="Guard", espn_id=102)])
    monkeypatch.setattr(DraftBoardService, "_latest_projections",
                        staticmethod(lambda season, source="espn": (None, state["projections"])))
    monkeypatch.setattr(module.DraftMarket, "latest_for_season",
                        classmethod(lambda cls, season, source="espn": state["market"]))
    monkeypatch.setattr(module.Player, "select",
                        classmethod(lambda cls, *fields: _Query(players.values())))
    monkeypatch.setattr(module.DraftPick, "select",
                        classmethod(lambda cls, *fields: _Query(state["picks"])))
    return state


def _market_row(player_id, **overrides):
    base = dict(player_id=player_id, as_of_date=date(2026, 9, 1), overall_rank=None,
                adp=None, auction_value=None, default_position_id=None,
                eligible_slot_ids=None, injury_status=None)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_fetch_builds_the_pool_and_reads_the_market_position_columns(fake_tables):
    fake_tables["market"] = [
        _market_row(1, overall_rank=1, adp=1.79, auction_value=65.0,
                    default_position_id=5, eligible_slot_ids=[4, 9, 11, 12, 13],
                    injury_status="DAY_TO_DAY"),
    ]

    inputs = DraftBoardService._fetch_inputs(frozenset(), None)

    assert inputs.season == SEASON
    assert {r.id for r in inputs.pool} == {1, 2}
    assert inputs.source == {1: "baseline", 2: "baseline"}
    assert inputs.last_season_gp == {1: 70, 2: 60}
    assert inputs.market_as_of == date(2026, 9, 1)
    assert inputs.market[1] == {
        "overall_rank": 1, "adp": 1.79, "auction_value": 65.0,
        "default_position_id": 5, "eligible_slot_ids": [4, 9, 11, 12, 13],
        "injury_status": "DAY_TO_DAY",
    }
    assert inputs.positions == {1: "C", 2: "G", 9: "F"}
    assert inputs.market_only == []


@pytest.mark.unit
def test_fetch_turns_a_ranked_player_with_no_stat_line_into_a_market_only_row(fake_tables):
    fake_tables["market"] = [_market_row(9, overall_rank=25), _market_row(1, overall_rank=1)]

    inputs = DraftBoardService._fetch_inputs(frozenset(), None)

    assert [(r.id, r.name, r.espn_id) for r in inputs.market_only] == [(9, "Rookie", 109)]


@pytest.mark.unit
def test_fetch_projections_win_over_the_baseline_and_carry_projected_games(fake_tables):
    fake_tables["projections"] = [
        SimpleNamespace(player_id=1, projected_gp=78, player=fake_tables["players"][1],
                        pts=30.0, reb=12.0, ast=10.0, stl=1.0, blk=0.7, tov=3.0,
                        fgm=10.0, fga=17.0, fg3m=1.0, fg3a=3.0, ftm=5.0, fta=6.0, min=34.0),
    ]

    inputs = DraftBoardService._fetch_inputs(frozenset(), None)

    assert inputs.source[1] == "projection" and inputs.source[2] == "baseline"
    assert inputs.projected_gp == {1: 78}
    projected = next(r for r in inputs.pool if r.id == 1)
    assert projected.line.pts == 30.0 and projected.gp == 78
    # The baseline row still supplies the NBA team a projection does not carry.
    assert projected.team == "DEN"


@pytest.mark.unit
def test_fetch_splits_a_sessions_picks_into_everyones_and_mine(fake_tables):
    fake_tables["picks"] = [
        SimpleNamespace(player_id=1, by_me=True),
        SimpleNamespace(player_id=2, by_me=False),
    ]

    inputs = DraftBoardService._fetch_inputs(frozenset(), 77)

    assert inputs.session_picked == frozenset({1, 2})
    assert inputs.session_mine == frozenset({1})
    # No session id, no pick query at all.
    assert DraftBoardService._fetch_inputs(frozenset(), None).session_picked == frozenset()


@pytest.mark.unit
def test_an_uncapped_espn_position_is_never_blocked_by_the_coarse_fallback(monkeypatch):
    """A candidate ESPN calls a PF cannot breach a centre cap, whatever
    nba_api lists him as — and that answer needs no roster counting, so one
    unknown roster player must not route him through the coarse rule."""
    # Only player 4 has ESPN data, so `roster_known` is False for a roster of 1.
    # Player 4 is a PF by ESPN and 'F-C' by nba_api; the league caps only C.
    market = _espn_market(**{"4": {"default_position_id": 4}})
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None:
                                     _inputs(market=market, positions={1: "C", 2: "G", 3: "F",
                                                                       4: "C-F", 5: None, 6: "C"})))
    resp = _board(resolve_scoring(_league(position_limits={"C": 1})), mine=[1])

    by_id = {r.player_id: r for r in resp.data}
    assert by_id[4].cap_blocked is False     # ESPN says PF; the C cap is not his
    assert by_id[6].cap_blocked is True      # no ESPN data: judged coarsely, and coarsely a C
