"""
DraftBoardService with the fetch layer stubbed (no DB): projection-over-baseline
composition, league-scored values for both formats, market join and delta,
stable ranks under picks, and the coarse position-cap check.
"""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from services.draft_board_service import BoardInputs, DraftBoardService
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
        matchup_periods={}, roster_slots={}, position_limits={}, draft_settings={},
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

    def fake_fetch(my_ids):
        calls.append(my_ids)
        return _inputs()

    monkeypatch.setattr(DraftBoardService, "_fetch_inputs", staticmethod(fake_fetch))
    return calls


def _board(scoring, picked=(), mine=()):
    return asyncio.run(DraftBoardService.get_board(scoring, picked_ids=picked, my_ids=mine))


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
    assert stub_inputs == [frozenset({1})]     # the fetch was told whose roster to look up


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
                        staticmethod(lambda my_ids: BoardInputs(season=SEASON, pool=[])))
    resp = _board(resolve_scoring(_league()))

    assert resp.status == ApiStatus.SUCCESS and resp.data == []
    assert SEASON in resp.message
    assert resp.meta.pool_size == 0 and resp.meta.available == 0
