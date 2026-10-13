"""SyncTTLCache: TTL, LRU bound, the kill switch, and single-flight across threads."""

import threading
import time

import pytest

from core.sync_cache import SyncTTLCache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(ttl=60.0, max_entries=4, clock=None, wait_timeout=5.0) -> SyncTTLCache:
    return SyncTTLCache("test", ttl_seconds=ttl, max_entries=max_entries, clock=clock or FakeClock(),
                        wait_timeout_seconds=wait_timeout)


def _counting(value):
    calls = []

    def build():
        calls.append(1)
        return value

    return build, calls


@pytest.mark.unit
def test_second_call_is_a_hit_and_does_not_build():
    cache = _cache()
    build, calls = _counting({"a": 1})

    first = cache.get_or_build("k", build)
    second = cache.get_or_build("k", build)

    assert calls == [1]
    assert first is second                       # the stored object itself, not a rebuild
    assert cache.stats() == {"name": "test", "entries": 1, "hits": 1, "misses": 1, "coalesced": 0, "wait_timeouts": 0}


@pytest.mark.unit
def test_an_entry_expires_after_the_ttl():
    clock = FakeClock()
    cache = _cache(ttl=300, clock=clock)
    cache.get_or_build("k", lambda: "one")

    clock.now += 299.9
    assert cache.get_or_build("k", lambda: "two") == "one"
    clock.now += 0.1                             # exactly the TTL old: expired
    assert cache.get_or_build("k", lambda: "two") == "two"
    assert cache.get_or_build("k", lambda: "three") == "two"   # the rebuild is stored afresh


@pytest.mark.unit
@pytest.mark.parametrize("ttl, max_entries", [(0, 4), (60, 0)])
def test_a_zero_ttl_or_bound_is_the_kill_switch(ttl, max_entries):
    cache = _cache(ttl=ttl, max_entries=max_entries)
    build, calls = _counting("v")

    assert not cache.enabled
    for _ in range(3):
        assert cache.get_or_build("k", build) == "v"
    assert calls == [1, 1, 1]
    assert cache.stats()["entries"] == 0 and cache.stats()["misses"] == 0


@pytest.mark.unit
def test_keys_are_independent_and_the_least_recently_used_is_evicted():
    cache = _cache(max_entries=2)
    cache.get_or_build("a", lambda: "a")
    cache.get_or_build("b", lambda: "b")
    cache.get_or_build("a", lambda: "a2")        # a hit: a becomes the most recently used
    cache.get_or_build("c", lambda: "c")         # evicts b

    assert cache.get_or_build("a", lambda: "rebuilt") == "a"
    assert cache.get_or_build("c", lambda: "rebuilt") == "c"
    assert cache.get_or_build("b", lambda: "rebuilt") == "rebuilt"


@pytest.mark.unit
def test_a_failed_build_raises_and_stores_nothing():
    cache = _cache()

    def boom():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        cache.get_or_build("k", boom)
    assert cache.get_or_build("k", lambda: "ok") == "ok"


@pytest.mark.unit
def test_clear_drops_entries_and_a_build_that_straddles_it_is_not_stored():
    cache = _cache()
    cache.get_or_build("k", lambda: "old")
    cache.clear()
    assert cache.get_or_build("k", lambda: "new") == "new"

    def build_then_clear():
        cache.clear()                            # e.g. a test resets between this build's read and its store
        return "straddled"

    cache.clear()
    assert cache.get_or_build("k", build_then_clear) == "straddled"   # its caller still gets it...
    assert cache.get_or_build("k", lambda: "fresh") == "fresh"        # ...but it was never stored


@pytest.mark.unit
def test_a_caller_after_a_clear_does_not_join_a_build_from_before_it():
    cache = _cache()
    started, release = threading.Event(), threading.Event()
    got = {}

    def old_build():
        started.set()
        release.wait(5)
        return "old"

    leader = threading.Thread(target=lambda: got.setdefault("leader", cache.get_or_build("k", old_build)))
    leader.start()
    assert started.wait(5)
    cache.clear()                                # e.g. a new snapshot announced mid-build

    assert cache.get_or_build("k", lambda: "new") == "new"            # its own build, not a wait on "old"
    release.set()
    leader.join(5)
    assert got["leader"] == "old"                # the caller that asked before the clear still gets it
    assert cache.get_or_build("k", lambda: "unused") == "new"         # ...but "old" never replaced "new"


def _burst(cache, n, build):
    """n threads released together, each asking for the same key."""
    barrier = threading.Barrier(n)
    results, errors = [None] * n, [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = cache.get_or_build("k", build)
        except Exception as exc:                 # noqa: BLE001 - recorded for the assertions
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    return results, errors


def _wait_for_followers(cache, n):
    """Hold the leader's build until every other thread is waiting on it, so the
    burst is a real single-flight however the threads get scheduled."""
    deadline = time.monotonic() + 5
    while cache.stats()["coalesced"] < n and time.monotonic() < deadline:
        time.sleep(0.001)


@pytest.mark.unit
def test_concurrent_misses_build_once_and_share_the_result():
    cache = _cache()
    calls = []
    lock = threading.Lock()

    def slow_build():
        with lock:
            calls.append(threading.get_ident())
        _wait_for_followers(cache, 9)
        return {"pool": 506}

    results, errors = _burst(cache, 10, slow_build)

    assert len(calls) == 1
    assert errors == [None] * 10
    assert all(r is results[0] for r in results)
    assert cache.stats() == {"name": "test", "entries": 1, "hits": 0, "misses": 1, "coalesced": 9, "wait_timeouts": 0}


@pytest.mark.unit
def test_when_the_leaders_build_fails_each_follower_builds_for_itself():
    cache = _cache()
    calls = []
    lock = threading.Lock()

    def flaky_build():
        with lock:
            calls.append(1)
            first = len(calls) == 1
        if first:
            _wait_for_followers(cache, 4)
            raise RuntimeError("connection reset")
        return "ok"

    results, errors = _burst(cache, 5, flaky_build)

    assert sum(isinstance(e, RuntimeError) for e in errors) == 1      # only the leader's caller sees it
    assert sorted(r for r in results if r is not None) == ["ok"] * 4
    assert len(calls) == 5                                            # in parallel, as without the cache
    assert cache.stats()["entries"] == 0                              # and none of them stored


@pytest.mark.unit
def test_a_follower_stops_waiting_on_a_stuck_build_and_builds_for_itself():
    cache = _cache(wait_timeout=0.05)
    started, release = threading.Event(), threading.Event()
    got = {}

    def stuck_build():
        started.set()
        release.wait(5)                          # e.g. a half-open socket the keepalives have not noticed yet
        return "stuck"

    leader = threading.Thread(target=lambda: got.setdefault("leader", cache.get_or_build("k", stuck_build)))
    leader.start()
    assert started.wait(5)

    began = time.monotonic()
    assert cache.get_or_build("k", lambda: "own") == "own"            # gave up on the leader and built alone
    assert time.monotonic() - began < 2
    assert cache.stats()["coalesced"] == 1 and cache.stats()["wait_timeouts"] == 1

    assert cache.get_or_build("k", lambda: "fresh") == "fresh"        # a later caller leads a fresh build...
    assert cache.stats()["wait_timeouts"] == 1                        # ...rather than queueing behind the stuck one
    release.set()
    leader.join(5)
    assert got["leader"] == "stuck"              # the stuck build still reaches its own caller
    assert cache.get_or_build("k", lambda: "unused") == "fresh"       # ...but its late result is never stored


@pytest.mark.unit
def test_followers_of_a_stuck_build_each_build_for_themselves():
    cache = _cache(wait_timeout=0.25)
    started, release = threading.Event(), threading.Event()
    calls = []
    lock = threading.Lock()

    def stuck_build():
        started.set()
        release.wait(5)
        return "stuck"

    def own_build():
        with lock:
            calls.append(1)
        return "own"

    got = {}
    leader = threading.Thread(target=lambda: got.setdefault("leader", cache.get_or_build("k", stuck_build)))
    leader.start()
    assert started.wait(5)
    results, errors = _burst(cache, 4, own_build)  # all four join the stuck build, then give up on it
    release.set()
    leader.join(5)

    assert errors == [None] * 4 and results == ["own"] * 4 and len(calls) == 4   # one build each, as uncached
    assert got["leader"] == "stuck"
    assert cache.stats()["coalesced"] == 4 and cache.stats()["wait_timeouts"] == 4
    assert cache.stats()["entries"] == 0                              # neither the stuck build nor theirs stored


@pytest.mark.unit
def test_the_default_wait_outlasts_any_healthy_build():
    assert SyncTTLCache("t", ttl_seconds=60, max_entries=1).wait_timeout_seconds == 30.0


@pytest.mark.unit
def test_a_stored_value_that_no_longer_serves_the_caller_is_rebuilt_and_replaced():
    cache = _cache()
    current = {"version": 1}

    def build():
        return {"built_from": current["version"]}

    def valid(value):
        return value["built_from"] == current["version"]

    first = cache.get_or_build("k", build, valid=valid)
    assert cache.get_or_build("k", build, valid=valid) is first      # still serves: a hit
    current["version"] = 2                                           # what it was derived from moved on
    second = cache.get_or_build("k", build, valid=valid)
    assert second == {"built_from": 2}
    assert cache.get_or_build("k", build, valid=valid) is second     # the rebuild replaced it
    assert cache.stats()["hits"] == 2 and cache.stats()["misses"] == 2


@pytest.mark.unit
def test_a_follower_the_leaders_build_does_not_serve_builds_for_itself():
    cache = _cache()
    started, release = threading.Event(), threading.Event()
    got = {}

    def old_build():
        started.set()
        release.wait(5)
        return "v1"

    leader = threading.Thread(target=lambda: got.setdefault("leader", cache.get_or_build("k", old_build)))
    leader.start()
    assert started.wait(5)

    def follower():
        got["follower"] = cache.get_or_build("k", lambda: "v2", valid=lambda v: v == "v2")

    t = threading.Thread(target=follower)
    t.start()
    _wait_for_followers(cache, 1)
    release.set()
    leader.join(5)
    t.join(5)

    assert got == {"leader": "v1", "follower": "v2"}
    assert cache.stats()["coalesced"] == 1 and cache.stats()["entries"] == 1   # the leader's build is the one stored
