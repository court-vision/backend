"""
The per-process category value cache in `PlayerValueService`: a category Week
open values players 10-13 times against the same ~506-row pool, so the pool is
loaded once per window and z-scored once per (window, category set), then every
valuation is a lookup. No DB: the pool loaders are stubbed and counted.
"""

import threading
import time
from datetime import date

import pytest

from core.settings import settings
from services import player_value_service as pvs
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.category_rank import PoolRow
from services.scoring.category_value import category_values as real_category_values
from services.scoring.models import CategoryDef, StatLine
from services.scoring.resolver import resolve_scoring
from services.scoring.vocab import DEFAULT_CATEGORIES

NINE_CAT = [CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]
EIGHT_CAT = [c for c in NINE_CAT if c.key != "tov"]


def _pool(n: int, seed: int) -> list[PoolRow]:
    """n distinct, deterministic rows; rows 0 and 1 share a name on different teams."""
    rows = []
    for i in range(n):
        k = (i * 7 + seed) % 23
        fga = 4 + k % 15
        fta = 1 + k % 6
        line = StatLine.from_dict(dict(
            pts=5 + k, reb=2 + k % 9, ast=1 + k % 7, stl=0.2 + (k % 5) / 3, blk=0.1 + (k % 4) / 3,
            tov=0.5 + k % 4, fg3m=k % 4, fga=fga, fgm=fga * (0.4 + (k % 5) / 25), fta=fta, ftm=fta * (0.6 + (k % 4) / 12),
        ))
        name = "Twin Name" if i < 2 else f"Player {i}"
        rows.append(PoolRow(id=1000 + i, name=name, team=("BOS", "MIA")[i % 2] if i < 2 else "LAL", gp=3 + i % 5,
                            line=line, fpts_avg=float(k), fpts_total=float(k * 4), espn_id=50000 + i,
                            name_normalized=name.lower()))
    return rows


ROLLING = _pool(40, seed=1)
BASELINE = _pool(30, seed=5)
ESPN_IDS = [50000 + i for i in range(0, 40, 3)] + [999]


def _league_scoring(cats=NINE_CAT):
    from types import SimpleNamespace
    from datetime import datetime

    league = SimpleNamespace(
        id=1, provider="espn", provider_league_id="123", season=2027, name="L",
        scoring_type="categories", categories=[c.to_json() for c in cats], point_weights={},
        category_win_mode=None, settings_synced_at=datetime(2026, 8, 24),
        raw_settings={}, matchup_periods={}, roster_slots={},
    )
    return resolve_scoring(league)


@pytest.fixture
def pools(monkeypatch):
    """Stub the loaders and count loads and z-scorings."""
    calls = {"rolling": 0, "baseline": 0, "scored": 0}
    state = {"rolling": list(ROLLING), "baseline": list(BASELINE), "hold": None}

    def fake_load_pool(window, season=None):
        calls["rolling"] += 1
        if state["hold"]:
            state["hold"]()
        return (date(2026, 11, 10) if state["rolling"] else None), list(state["rolling"])

    def fake_baseline(season=None):
        calls["baseline"] += 1
        return list(state["baseline"])

    def counted_category_values(pool, cat_defs, min_games=1):
        calls["scored"] += 1
        return real_category_values(pool, cat_defs, min_games)

    monkeypatch.setattr(pvs, "load_pool", fake_load_pool)
    monkeypatch.setattr(pvs, "load_baseline_pool", fake_baseline)
    monkeypatch.setattr(pvs, "category_values", counted_category_values)
    return type("Pools", (), {"calls": calls, "state": state})


def _disable_cache(monkeypatch):
    monkeypatch.setattr(pvs._POOL_CACHE, "ttl_seconds", 0.0)
    monkeypatch.setattr(pvs._SCORED_CACHE, "ttl_seconds", 0.0)


def _every_valuation(scoring):
    """Each shape a provider asks for: ESPN ids and names, the window, `recent`."""
    names = [("Twin Name", "MIA"), ("Twin Name", "BOS"), ("twin name", "XXX"), ("Player 7", "LAL"), ("Nobody", "SAS")]
    return {
        "espn": PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS),
        "espn_7": PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS, days=7),
        "espn_recent": PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS, recent=True),
        "names": PlayerValueService.avg_points_for(scoring, names=names),
        "by_team": {t: PlayerValueService.category_value_by_name([("Twin Name", t)], NINE_CAT)
                    for t in ("MIA", "BOS", "XXX")},
    }


# ---- one load and one z-scoring per Week open ----------------------------------------


@pytest.mark.unit
def test_a_week_opens_valuations_load_and_score_the_pool_once(pools):
    scoring = _league_scoring()
    first = PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS)
    for i in range(11):                          # /lineup per day, the matchup trio, /actions, streamers
        out = PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS[i % 4:])
        assert out == {eid: first[eid] for eid in ESPN_IDS[i % 4:]}
    assert pools.calls == {"rolling": 1, "baseline": 0, "scored": 1}


@pytest.mark.unit
@pytest.mark.parametrize("source", ["rolling", "baseline", "empty"])
def test_cached_values_are_identical_to_the_uncached_path(pools, monkeypatch, source):
    if source != "rolling":
        pools.state["rolling"] = []              # pre-season: last season's baseline pool
    if source == "empty":
        pools.state["baseline"] = []
    scoring = _league_scoring()

    cold = _every_valuation(scoring)
    warm = _every_valuation(scoring)             # every call a hit
    _disable_cache(monkeypatch)
    uncached = _every_valuation(scoring)

    assert cold == warm == uncached
    if source == "rolling":
        assert {r.source for r in cold["espn"].values()} == {"rolling", None}
    elif source == "baseline":
        assert {r.source for r in cold["espn"].values()} == {"baseline", None}
    else:
        assert all(r == ValueResult(None, None) for r in cold["espn"].values())


@pytest.mark.unit
def test_category_sets_share_one_pool_and_are_scored_separately(pools):
    nine = PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)
    eight = PlayerValueService.category_value_by_espn_id(ESPN_IDS, EIGHT_CAT)
    relabelled = [CategoryDef(c.key, c.label.lower(), c.higher_is_better, c.is_rate) for c in NINE_CAT]
    assert PlayerValueService.category_value_by_espn_id(ESPN_IDS, relabelled) == nine   # labels do not matter
    flipped = [CategoryDef.for_key(c.key, higher_is_better=True) if c.key == "tov" else c for c in NINE_CAT]
    punt_tov = PlayerValueService.category_value_by_espn_id(ESPN_IDS, flipped)          # direction does

    assert nine != eight and nine != punt_tov
    assert pools.calls == {"rolling": 1, "baseline": 0, "scored": 3}
    expected = real_category_values(ROLLING, EIGHT_CAT)
    by_espn = {row.espn_id: expected[row.id][0] for row in ROLLING}
    assert {eid: r.value for eid, r in eight.items()} == {eid: by_espn.get(eid) for eid in ESPN_IDS}


@pytest.mark.unit
def test_windows_and_seasons_are_separate_entries(pools, monkeypatch):
    for days in (7, 14, 30, 3, 10, 22):
        PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT, days=days)
    assert pools.calls["rolling"] == 3           # 3 and 10 snap to 7, 22 to 14

    monkeypatch.setattr(settings, "nba_season", "2027-28")
    PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)
    assert pools.calls["rolling"] == 4           # the baseline pool is the season before this one


# ---- staleness: the TTL, and the kill switch ------------------------------------------------


@pytest.mark.unit
def test_a_new_snapshot_is_picked_up_once_the_ttl_runs_out(pools, monkeypatch):
    now = [1000.0]
    for cache in (pvs._POOL_CACHE, pvs._SCORED_CACHE):
        monkeypatch.setattr(cache, "_clock", lambda: now[0])
    before = PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)

    pools.state["rolling"] = list(reversed(_pool(40, seed=11)))     # post-game wrote a new snapshot
    now[0] += settings.category_value_cache_ttl_seconds - 1
    assert PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT) == before
    now[0] += 1
    after = PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)

    assert after != before
    expected = real_category_values(pools.state["rolling"], NINE_CAT)
    assert after[50003].value == expected[1003][0]
    assert pools.calls["rolling"] == 2


def _expected(pool, cats, espn_ids=ESPN_IDS):
    values = real_category_values(pool, cats)
    by_espn = {row.espn_id: values[row.id][0] for row in pool if row.id in values}
    return {eid: ValueResult(by_espn[eid], "rolling") if eid in by_espn else ValueResult(None, None) for eid in espn_ids}


@pytest.mark.unit
def test_values_scored_late_in_a_loads_life_refresh_with_the_load(pools, monkeypatch):
    """A scored entry is tied to the load it came from: the 8-cat values scored at
    TTL-1 off the 9-cat league's load must move to the new snapshot when that load
    does (TTL+1), not a whole TTL after they were scored."""
    from types import SimpleNamespace

    now = [1000.0]
    for cache in (pvs._POOL_CACHE, pvs._SCORED_CACHE):
        monkeypatch.setattr(cache, "_clock", lambda: now[0])
    proj = SimpleNamespace(player_id=ROLLING[3].id, projected_gp=60,
                           player=SimpleNamespace(name="Player 3", name_normalized="player 3"),
                           **{k: getattr(ROLLING[0].line, k) * 1.2 for k in StatLine.ROW_KEYS})
    monkeypatch.setattr(PlayerValueService, "projections_by_espn_id",
                        staticmethod(lambda espn_ids, season=None: {50003: proj}))
    eight_league = _league_scoring(EIGHT_CAT)
    ttl = settings.category_value_cache_ttl_seconds

    PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)                 # t=0: the load, 9-cat scored
    now[0] += ttl - 1
    eight_before = PlayerValueService.category_value_by_espn_id(ESPN_IDS, EIGHT_CAT)  # t=TTL-1: 8-cat scored off it
    projected_before = PlayerValueService.projected_value_by_espn_id(eight_league, [50003])
    assert eight_before == _expected(ROLLING, EIGHT_CAT)

    pools.state["rolling"] = list(reversed(_pool(40, seed=11)))     # post-game writes a new snapshot
    now[0] += 2                                                      # t=TTL+1: the load has expired
    eight_after = PlayerValueService.category_value_by_espn_id(ESPN_IDS, EIGHT_CAT)
    projected_after = PlayerValueService.projected_value_by_espn_id(eight_league, [50003])
    nine_after = PlayerValueService.category_value_by_espn_id(ESPN_IDS, NINE_CAT)

    assert eight_after != eight_before and eight_after == _expected(pools.state["rolling"], EIGHT_CAT)
    assert nine_after == _expected(pools.state["rolling"], NINE_CAT)
    assert pools.calls == {"rolling": 2, "baseline": 0, "scored": 4}   # one reload, each category set rescored once

    _disable_cache(monkeypatch)                                      # board values and projection: one snapshot
    assert PlayerValueService.category_value_by_espn_id(ESPN_IDS, EIGHT_CAT) == eight_after
    assert PlayerValueService.projected_value_by_espn_id(eight_league, [50003]) == projected_after != projected_before


@pytest.mark.unit
def test_the_kill_switch_loads_and_scores_on_every_call(pools, monkeypatch):
    _disable_cache(monkeypatch)
    scoring = _league_scoring()
    for _ in range(3):
        PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS)
    assert pools.calls == {"rolling": 3, "baseline": 0, "scored": 3}


@pytest.mark.unit
def test_the_settings_reach_the_caches_and_cannot_be_negative():
    from pydantic import ValidationError

    from core.settings import Settings

    assert Settings.model_fields["category_value_cache_ttl_seconds"].default == 300.0
    for cache in (pvs._POOL_CACHE, pvs._SCORED_CACHE):
        assert cache.ttl_seconds == settings.category_value_cache_ttl_seconds
        assert cache.max_entries == settings.category_value_cache_max_entries
    assert Settings(category_value_cache_ttl_seconds=0).category_value_cache_ttl_seconds == 0   # the kill switch boots
    with pytest.raises(ValidationError, match="category value cache"):
        Settings(category_value_cache_ttl_seconds=-1)
    with pytest.raises(ValidationError, match="category value cache"):
        Settings(category_value_cache_max_entries=-1)


# ---- concurrency: callers are DB-executor threads ------------------------------------------


@pytest.mark.unit
def test_concurrent_valuations_build_the_pool_once(pools):
    n = 10

    def hold():
        """Keep the first load going until every other thread waits on it."""
        deadline = time.monotonic() + 5
        while pvs._POOL_CACHE.stats()["coalesced"] - pool_before["coalesced"] < n - 1 and time.monotonic() < deadline:
            time.sleep(0.001)

    pool_before, scored_before = pvs._POOL_CACHE.stats(), pvs._SCORED_CACHE.stats()   # counters span tests
    pools.state["hold"] = hold
    scoring = _league_scoring()
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(r == results[0] for r in results) and results[0][50000].value is not None
    assert pools.calls == {"rolling": 1, "baseline": 0, "scored": 1}
    assert pvs._POOL_CACHE.stats()["coalesced"] - pool_before["coalesced"] == n - 1   # all waited on the one load...
    scored = {k: v - scored_before[k] for k, v in pvs._SCORED_CACHE.stats().items() if k != "name"}
    assert scored["misses"] == 1 and scored["hits"] + scored["coalesced"] == n - 1     # ...and shared one scoring


# ---- what callers get back cannot corrupt the shared copy ---------------------------------


@pytest.mark.unit
def test_callers_cannot_corrupt_the_cached_pool_or_values(pools):
    scoring = _league_scoring()
    first = PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS)
    reference = dict(first)
    first.clear()                                # a caller reusing its result dict

    source, pool = PlayerValueService._category_pool(14)
    assert source == "rolling" and len(pool) == len(ROLLING)
    pool.clear()                                 # ...or the pool list

    scored = PlayerValueService._category_scored(NINE_CAT, 14)
    with pytest.raises(TypeError):
        scored.values[1000] = 99.0               # the shared mappings are read-only
    with pytest.raises(TypeError):
        scored.by_espn[50000] = 99.0

    assert PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS) == reference
    assert len(PlayerValueService._category_pool(14)[1]) == len(ROLLING)
    assert pools.calls["rolling"] == 1


# ---- the projection path reads the same pool -------------------------------------------------


@pytest.mark.unit
def test_the_projection_path_reuses_the_cached_pool(pools, monkeypatch):
    from types import SimpleNamespace

    proj = SimpleNamespace(player_id=ROLLING[3].id, projected_gp=60,
                           player=SimpleNamespace(name="Player 3", name_normalized="player 3"),
                           **{k: getattr(ROLLING[0].line, k) * 1.2 for k in StatLine.ROW_KEYS})
    monkeypatch.setattr(PlayerValueService, "projections_by_espn_id",
                        staticmethod(lambda espn_ids, season=None: {50003: proj}))
    scoring = _league_scoring()

    PlayerValueService.avg_points_for(scoring, espn_ids=ESPN_IDS)
    cached = PlayerValueService.projected_value_by_espn_id(scoring, [50003])
    assert pools.calls["rolling"] == 1           # no second load for the projection

    _disable_cache(monkeypatch)
    assert PlayerValueService.projected_value_by_espn_id(scoring, [50003]) == cached
    assert pools.calls["rolling"] == 2 and cached[50003] > 0


@pytest.mark.unit
def test_a_points_league_never_touches_the_category_cache(pools, monkeypatch):
    monkeypatch.setattr(PlayerValueService, "rolling_avg_by_espn_id", staticmethod(lambda ids, days, weights: {1: 30.0}))
    out = PlayerValueService.avg_points_for(resolve_scoring(None), espn_ids=[1])
    assert out == {1: ValueResult(30.0, "rolling")}
    assert pvs._POOL_CACHE.stats()["entries"] == pvs._SCORED_CACHE.stats()["entries"] == 0
    assert pools.calls == {"rolling": 0, "baseline": 0, "scored": 0}
