"""
Integration: the per-process category value cache against the real pool queries.

A category league values players against the whole rolling pool (last season's
baseline pool before opening night). `PlayerValueService` keeps that pool and
its z-scores per process for `category_value_cache_ttl_seconds`; these check,
on the real migration chain, that a cached valuation is the uncached one, that
it really does stop reading the tables, and that DB-executor threads sharing it
get one build between them.
"""

import threading
from datetime import date, timedelta

import pytest

from core.settings import settings
from core.season import previous_season
from db.base import db
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from services import player_value_service as pvs
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.resolver import resolve_scoring

TODAY = date.today()
BASELINE_SEASON = previous_season(settings.nba_season)
ESPN_IDS = [9000 + pid for pid in range(1, 13)] + [9999]


def _seed(players: int = 12, rolling: bool = True) -> None:
    for pid in range(1, players + 1):
        Player.create(id=pid, name=f"P{pid}", name_normalized=f"p{pid}", espn_id=9000 + pid)
        pts = 6 + 2 * pid
        if rolling and pid <= players - 2:          # the last two have no game in the window yet
            for window in (7, 14, 30):
                PlayerRollingStats.create(
                    player=pid, as_of_date=TODAY, window_days=window, gp=2 + pid % 4, fpts=pts * 1.5,
                    pts=pts, reb=pts / 4 + window / 30, ast=(pid % 5) + 1, stl=0.5 + pid % 3 / 2, blk=pid % 4 / 3,
                    tov=1 + pid % 3, min=30.0, fgm=pts / 2.5, fga=pts / 1.2, fg_pct=0.48,
                    fg3m=pid % 4, fg3a=3.0 + pid % 3, fg3_pct=0.33, ftm=1 + pid % 3, fta=2 + pid % 4, ft_pct=0.75,
                )
        gp = 40 + pid
        PlayerSeasonStats.create(
            player=pid, team=None, season=BASELINE_SEASON, as_of_date=date(2026, 4, 12), gp=gp,
            fpts=gp * pts, pts=gp * pts, reb=gp * (3 + pid % 6), ast=gp * (1 + pid % 7), stl=gp, blk=gp * (pid % 3),
            tov=gp * 2, min=gp * 30, fgm=gp * 5, fga=gp * (9 + pid % 5), fg3m=gp * (pid % 4), fg3a=gp * 4,
            ftm=gp * 2, fta=gp * 3,
        )


def _uncached(monkeypatch):
    monkeypatch.setattr(pvs._POOL_CACHE, "ttl_seconds", 0.0)
    monkeypatch.setattr(pvs._SCORED_CACHE, "ttl_seconds", 0.0)


def _valuations():
    cats = resolve_scoring(None, "categories")
    points = resolve_scoring(None)
    return {
        "categories": PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS),
        "categories_recent": PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS, recent=True),
        "categories_by_name": PlayerValueService.avg_points_for(cats, names=[("P3", None), ("p4", None), ("Nobody", None)]),
        "projection_pool": [row.id for row in PlayerValueService._category_pool(14)[1]],
        "points": PlayerValueService.avg_points_for(points, espn_ids=ESPN_IDS),
    }


@pytest.mark.integration
@pytest.mark.parametrize("rolling", [True, False], ids=["rolling", "baseline"])
def test_cached_valuations_match_the_uncached_queries(integration_db, monkeypatch, rolling):
    _seed(rolling=rolling)

    cold, warm = _valuations(), _valuations()
    _uncached(monkeypatch)
    uncached = _valuations()

    assert cold == warm == uncached
    source = "rolling" if rolling else "baseline"
    assert cold["categories"][9001].source == source and cold["categories"][9001].value is not None
    assert cold["categories"][9999] == ValueResult(None, None)
    if rolling:
        assert cold["categories"][9012] == ValueResult(None, None)     # no game in the window yet


@pytest.mark.integration
def test_a_cached_pool_stops_reading_the_tables_until_it_is_dropped(integration_db):
    _seed()
    cats = resolve_scoring(None, "categories")
    before = PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS)

    PlayerRollingStats.delete().execute()                              # the snapshot is gone from the table...
    assert PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS) == before    # ...but not from the cache

    PlayerValueService.clear_category_cache()
    after = PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS)
    assert after[9001].source == "baseline"                            # rebuilt from what the tables hold now


@pytest.mark.integration
def test_db_threads_valuing_at_once_share_one_build(integration_db):
    _seed()
    cats = resolve_scoring(None, "categories")
    expected = PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS)
    PlayerValueService.clear_category_cache()
    misses = pvs._POOL_CACHE.stats()["misses"], pvs._SCORED_CACHE.stats()["misses"]

    n = 8
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def worker(i):
        try:
            barrier.wait()
            with db.connection_context():                              # what run_db does on its executor
                results[i] = PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS)
        except Exception as exc:                                       # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [] and all(r == expected for r in results)
    assert pvs._POOL_CACHE.stats()["misses"] - misses[0] == 1
    assert pvs._SCORED_CACHE.stats()["misses"] - misses[1] == 1


@pytest.mark.integration
def test_a_stale_rolling_snapshot_still_falls_back_to_the_baseline(integration_db):
    """The cache keys on the window, not on a snapshot date, so the freshness rule
    is still the loader's: last April's L14 read in October is no pool at all."""
    _seed()
    PlayerRollingStats.update(as_of_date=TODAY - timedelta(days=120)).execute()
    cats = resolve_scoring(None, "categories")
    out = PlayerValueService.avg_points_for(cats, espn_ids=ESPN_IDS)
    assert out[9001].source == "baseline" and out[9012].source == "baseline"
