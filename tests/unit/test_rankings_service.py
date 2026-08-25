"""
RankingsService category path with the pool loader stubbed (no DB): min_games
defaults, response assembly, and bad-input handling.
"""

import asyncio
from datetime import date

import pytest

from schemas.common import ApiStatus
from services.rankings_service import DEFAULT_MIN_GAMES, RankingsService
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.vocab import DEFAULT_CATEGORIES


def _row(id: int, gp: int, fpts: float, **stats) -> PoolRow:
    return PoolRow(id=id, name=f"P{id}", team="DEN" if id % 2 else None, gp=gp,
                   line=StatLine.from_dict(stats), fpts_avg=fpts, fpts_total=round(fpts * gp, 1))


POOL = [
    _row(1, gp=10, fpts=50.0, pts=30, reb=10, ast=8, stl=1.5, blk=1, tov=4, fg3m=3, fgm=11, fga=20, ftm=5, fta=6),
    _row(2, gp=9, fpts=40.0, pts=22, reb=6, ast=4, stl=1, blk=0.5, tov=2, fg3m=2, fgm=8, fga=16, ftm=4, fta=5),
    _row(3, gp=8, fpts=30.0, pts=15, reb=12, ast=2, stl=0.5, blk=2.5, tov=1.5, fg3m=0, fgm=6, fga=10, ftm=2, fta=4),
    _row(4, gp=2, fpts=70.0, pts=40, reb=15, ast=10, stl=3, blk=3, tov=1, fg3m=5, fgm=15, fga=25, ftm=8, fta=8),
]


@pytest.fixture
def stub_pool(monkeypatch):
    calls = []

    def fake_load_pool(window):
        calls.append(window)
        return date(2026, 3, 4), list(POOL)

    monkeypatch.setattr(RankingsService, "_load_pool", staticmethod(fake_load_pool))
    return calls


@pytest.mark.unit
def test_category_rankings_default_nine_cat_and_window_min_games(stub_pool):
    resp = asyncio.run(RankingsService.get_rankings(window=14, format="categories"))

    assert resp.status == ApiStatus.SUCCESS
    assert stub_pool == [14]
    # gp=2 player is filtered by the L14 default floor of 4 despite the best raw numbers
    assert {p.id for p in resp.data} == {1, 2, 3}
    assert [p.rank for p in resp.data] == [1, 2, 3]
    assert [p.score for p in resp.data] == sorted((p.score for p in resp.data), reverse=True)
    assert resp.data[0].id == 1                                        # best in 7 of 9 categories
    assert resp.meta.format == "categories" and resp.meta.window == 14
    assert resp.meta.as_of == date(2026, 3, 4)
    assert resp.meta.pool_size == 3 and resp.meta.min_games == DEFAULT_MIN_GAMES[14] == 4
    assert [c.key for c in resp.meta.categories] == DEFAULT_CATEGORIES

    top = resp.data[0]
    assert top.gp == 10 and top.avg_fpts == 50.0 and top.total_fpts == 500.0 and top.team == "DEN"
    assert set(top.categories) == set(DEFAULT_CATEGORIES) and set(top.category_z) == set(DEFAULT_CATEGORIES)
    assert top.categories["fg_pct"] == pytest.approx(0.55)
    assert top.score == pytest.approx(sum(top.category_z.values()), abs=2e-3)
    by_id = {p.id: p for p in resp.data}
    assert by_id[2].team == ""              # None team is rendered as empty string, as the points path does
    assert "as of 2026-03-04" in resp.message


@pytest.mark.unit
def test_category_rankings_explicit_categories_and_min_games(stub_pool):
    resp = asyncio.run(RankingsService.get_rankings(window=None, format="categories",
                                                    categories=["pts", "tov", "pts"], min_games=1))

    assert stub_pool == [None]
    assert [c.key for c in resp.meta.categories] == ["pts", "tov"]     # deduped, order kept
    assert resp.meta.min_games == 1 and resp.meta.pool_size == 4
    assert resp.data[0].id == 4                                        # eligible now, and the best scorer
    assert set(resp.data[0].categories) == {"pts", "tov"}


@pytest.mark.unit
def test_season_default_min_games_is_twenty(stub_pool):
    resp = asyncio.run(RankingsService.get_rankings(format="categories"))
    assert resp.meta.min_games == 20
    assert resp.data == [] and resp.meta.pool_size == 0
    assert "No Season data" in resp.message


@pytest.mark.unit
def test_bad_inputs_are_bad_request_not_500(stub_pool):
    unknown = asyncio.run(RankingsService.get_rankings(format="categories", categories=["dd"]))
    assert unknown.status == ApiStatus.BAD_REQUEST and "dd" in unknown.message and unknown.data == []

    bad_window = asyncio.run(RankingsService.get_rankings(window=12, format="categories"))
    assert bad_window.status == ApiStatus.BAD_REQUEST and stub_pool == []

    bad_format = asyncio.run(RankingsService.get_rankings(format="roto"))
    assert bad_format.status == ApiStatus.BAD_REQUEST
