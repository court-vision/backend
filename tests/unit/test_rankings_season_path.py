"""
RankingsService points/season path: the materialized nba.rankings, its
staleness fallback, and where the CPU work runs.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from db import base as db_base
from db.models.stats import rankings as rankings_models
from schemas.common import ApiStatus
from services import rankings_service
from services.rankings_service import RankingsService


def _row(id: int, rank: int, fpts: int, gp: int, as_of: date, season="2025-26"):
    return SimpleNamespace(
        id=id, curr_rank=rank, name=f"P{id}", team="DEN", fpts=fpts,
        avg_fpts=round(fpts / gp, 2), rank_change=0, gp=gp, as_of_date=as_of, season=season,
    )


COPY_ROWS = [_row(1, 1, 2000, 40, date(2026, 3, 4)), _row(2, 2, 1500, 38, date(2026, 3, 3))]
SOURCE_ROWS = [_row(1, 1, 2100, 41, date(2026, 3, 5)), _row(2, 2, 1500, 38, date(2026, 3, 3))]


class _Query(list):
    """Enough of a peewee query for `list(Model.select().order_by(...))`."""

    def order_by(self, *_args):
        return self


@pytest.fixture(autouse=True)
def direct_db_boundary(monkeypatch):
    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_base, "run_db", direct_run_db)


@pytest.fixture
def stub_tables(monkeypatch):
    """Stub both rankings relations and the season-stats freshness probe."""
    state = {"copy": COPY_ROWS, "source": SOURCE_ROWS, "source_as_of": date(2026, 3, 4), "reads": []}

    def copy_select(*_a, **_k):
        state["reads"].append("rankings")
        return _Query(state["copy"])

    def source_select(*_a, **_k):
        state["reads"].append("rankings_source")
        return _Query(state["source"])

    monkeypatch.setattr(rankings_models.Rankings, "select", staticmethod(copy_select))
    monkeypatch.setattr(rankings_models.RankingsSource, "select", staticmethod(source_select))
    monkeypatch.setattr(RankingsService, "_latest_season_date", staticmethod(lambda: state["source_as_of"]))
    return state


@pytest.mark.unit
def test_a_current_copy_is_read_and_the_source_view_is_not(stub_tables):
    resp = asyncio.run(RankingsService.get_rankings())

    assert stub_tables["reads"] == ["rankings"]
    assert resp.status == ApiStatus.SUCCESS
    assert [p.id for p in resp.data] == [1, 2]
    assert resp.message == "Rankings fetched successfully"


@pytest.mark.unit
def test_gp_season_and_as_of_come_off_the_row_itself(stub_tables):
    """Migration 0006 carries them, replacing two extra scans of nba.player_season_stats."""
    resp = asyncio.run(RankingsService.get_rankings())

    assert [p.gp for p in resp.data] == [40, 38]
    assert resp.meta.as_of == date(2026, 3, 4)
    assert resp.meta.season == "2025-26"
    assert resp.meta.max_gp == 40
    assert resp.meta.season_day == 135
    assert resp.data[0].total_fpts == 2000.0 and resp.data[0].avg_fpts == 50.0


@pytest.mark.unit
def test_a_lagging_copy_falls_back_to_the_source_view(stub_tables):
    """A missed refresh must cost latency, not correctness."""
    stub_tables["source_as_of"] = date(2026, 3, 5)      # the table moved on; the copy did not

    resp = asyncio.run(RankingsService.get_rankings())

    assert stub_tables["reads"] == ["rankings", "rankings_source"]
    assert resp.data[0].total_fpts == 2100.0            # source-view numbers
    assert resp.meta.as_of == date(2026, 3, 5)


@pytest.mark.unit
def test_an_unpopulated_copy_falls_back_too(stub_tables):
    stub_tables["copy"] = []

    resp = asyncio.run(RankingsService.get_rankings())

    assert stub_tables["reads"] == ["rankings", "rankings_source"]
    assert len(resp.data) == 2


@pytest.mark.unit
def test_no_season_data_at_all_is_an_empty_success_not_a_fallback_loop(stub_tables):
    stub_tables["copy"] = []
    stub_tables["source_as_of"] = None                  # nothing in nba.player_season_stats

    resp = asyncio.run(RankingsService.get_rankings())

    assert stub_tables["reads"] == ["rankings"]
    assert resp.status == ApiStatus.SUCCESS and resp.data == []
    assert resp.message == "No 2025-26 season data yet — rankings start after opening night"
    assert resp.meta.pool_size == 0


@pytest.mark.unit
def test_response_assembly_does_not_hold_a_database_permit(monkeypatch, stub_tables):
    """PRODUCTION_READINESS item 2: pure-CPU work must not sit on a DB permit."""
    state = {"in_db": False, "built_inside_db": None}

    async def tracking_run_db(operation_name, fn, *args, **kwargs):
        state["in_db"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            state["in_db"] = False

    monkeypatch.setattr(db_base, "run_db", tracking_run_db)
    build = RankingsService._build_season_response

    def spy(rows, basis):
        state["built_inside_db"] = state["in_db"]
        return build(rows, basis)

    monkeypatch.setattr(RankingsService, "_build_season_response", staticmethod(spy))

    assert asyncio.run(RankingsService.get_rankings()).status == ApiStatus.SUCCESS
    assert state["built_inside_db"] is False


@pytest.mark.unit
def test_category_scoring_does_not_hold_a_database_permit(monkeypatch):
    """The 3.4x-under-concurrency case: z-scoring is pure CPU and zero database work."""
    from services.scoring.category_rank import PoolRow
    from services.scoring.models import StatLine

    pool = [
        PoolRow(id=1, name="A", team="DEN", gp=10, line=StatLine.from_dict({"pts": 30, "reb": 10}),
                fpts_avg=50.0, fpts_total=500.0),
        PoolRow(id=2, name="B", team="OKC", gp=9, line=StatLine.from_dict({"pts": 20, "reb": 5}),
                fpts_avg=40.0, fpts_total=360.0),
    ]
    monkeypatch.setattr(RankingsService, "_load_pool", staticmethod(lambda window: (date(2026, 3, 4), pool)))

    state = {"in_db": False, "scored_inside_db": None}

    async def tracking_run_db(operation_name, fn, *args, **kwargs):
        state["in_db"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            state["in_db"] = False

    monkeypatch.setattr(db_base, "run_db", tracking_run_db)
    score = rankings_service.compute_category_scores

    def spy(eligible, cat_defs):
        state["scored_inside_db"] = state["in_db"]
        return score(eligible, cat_defs)

    monkeypatch.setattr(rankings_service, "compute_category_scores", spy)

    resp = asyncio.run(RankingsService.get_rankings(window=14, format="categories", categories=["pts", "reb"]))

    assert resp.status == ApiStatus.SUCCESS and len(resp.data) == 2
    assert state["scored_inside_db"] is False
