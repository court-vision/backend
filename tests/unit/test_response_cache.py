"""ResponseCache: TTL, LRU bound, single-flight, and what is deliberately not cached."""

import asyncio

import pytest

from core.cache import CachedResponse, ResponseCache


def _cache(ttl=60.0, max_entries=4) -> ResponseCache:
    return ResponseCache("test", ttl_seconds=ttl, max_entries=max_entries)


async def _render(body: bytes):
    return body


@pytest.mark.unit
def test_second_call_is_served_from_the_cache_without_rendering():
    cache = _cache()
    renders = []

    async def render():
        renders.append(1)
        return b'{"a":1}'

    async def scenario():
        first = await cache.get_or_render("k", render)
        second = await cache.get_or_render("k", render)
        return first, second

    first, second = asyncio.run(scenario())
    assert renders == [1]
    assert first.body == second.body == b'{"a":1}'
    assert first.etag == second.etag and first.etag.startswith('"')
    assert (cache.hits, cache.misses) == (1, 1)


@pytest.mark.unit
def test_entry_expires_after_the_ttl():
    cache = _cache(ttl=0.05)

    async def scenario():
        await cache.get_or_render("k", lambda: _render(b"one"))
        await asyncio.sleep(0.06)
        return await cache.get_or_render("k", lambda: _render(b"two"))

    assert asyncio.run(scenario()).body == b"two"
    assert cache.get("k") is not None          # the fresh render replaced it


@pytest.mark.unit
def test_keys_are_independent_and_the_oldest_is_evicted_past_the_bound():
    cache = _cache(max_entries=2)

    async def scenario():
        for key in ("a", "b", "c"):
            await cache.get_or_render(key, lambda k=key: _render(k.encode()))

    asyncio.run(scenario())
    assert cache.get("a") is None              # evicted: least recently used
    assert cache.get("b").body == b"b"
    assert cache.get("c").body == b"c"


@pytest.mark.unit
def test_reading_an_entry_makes_it_the_most_recently_used():
    cache = _cache(max_entries=2)

    async def scenario():
        await cache.get_or_render("a", lambda: _render(b"a"))
        await cache.get_or_render("b", lambda: _render(b"b"))
        cache.get("a")                          # refresh a's position
        await cache.get_or_render("c", lambda: _render(b"c"))

    asyncio.run(scenario())
    assert cache.get("a") is not None
    assert cache.get("b") is None


@pytest.mark.unit
def test_concurrent_misses_on_one_key_render_once_and_share_the_result():
    """The stampede at expiry is the thing the cache exists to prevent."""
    cache = _cache()
    renders = []

    async def slow_render():
        renders.append(1)
        await asyncio.sleep(0.05)
        return b"shared"

    async def scenario():
        return await asyncio.gather(*(cache.get_or_render("k", slow_render) for _ in range(10)))

    results = asyncio.run(scenario())
    assert renders == [1]
    assert {r.body for r in results} == {b"shared"}
    assert cache.coalesced == 9


@pytest.mark.unit
def test_a_failed_render_is_not_cached_and_reaches_every_waiter():
    cache = _cache()
    attempts = []

    async def failing_render():
        attempts.append(1)
        await asyncio.sleep(0.01)
        raise RuntimeError("nope")

    async def scenario():
        results = await asyncio.gather(
            *(cache.get_or_render("k", failing_render) for _ in range(3)),
            return_exceptions=True,
        )
        # the next caller retries rather than inheriting the failure
        return results, await cache.get_or_render("k", lambda: _render(b"ok"))

    results, recovered = asyncio.run(scenario())
    assert attempts == [1]
    assert all(isinstance(r, RuntimeError) for r in results)
    assert recovered.body == b"ok"


@pytest.mark.unit
def test_a_cancelled_leader_does_not_wedge_the_key():
    cache = _cache()

    async def scenario():
        started = asyncio.Event()

        async def slow_render():
            started.set()
            await asyncio.sleep(10)
            return b"never"

        leader = asyncio.create_task(cache.get_or_render("k", slow_render))
        await started.wait()
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        return await cache.get_or_render("k", lambda: _render(b"after"))

    assert asyncio.run(scenario()).body == b"after"


@pytest.mark.unit
def test_a_follower_giving_up_does_not_cancel_the_shared_render():
    cache = _cache()

    async def scenario():
        started = asyncio.Event()

        async def slow_render():
            started.set()
            await asyncio.sleep(0.05)
            return b"done"

        leader = asyncio.create_task(cache.get_or_render("k", slow_render))
        await started.wait()
        follower = asyncio.create_task(cache.get_or_render("k", slow_render))
        await asyncio.sleep(0)
        follower.cancel()
        return await leader

    assert asyncio.run(scenario()).body == b"done"


@pytest.mark.unit
def test_a_zero_ttl_disables_the_cache_entirely():
    cache = _cache(ttl=0)
    renders = []

    async def render():
        renders.append(1)
        return b"x"

    async def scenario():
        await cache.get_or_render("k", render)
        await cache.get_or_render("k", render)

    asyncio.run(scenario())
    assert cache.enabled is False
    assert renders == [1, 1]
    assert cache.get("k") is None


@pytest.mark.unit
def test_etag_is_stable_for_identical_bytes_and_differs_otherwise():
    assert CachedResponse.of(b"same").etag == CachedResponse.of(b"same").etag
    assert CachedResponse.of(b"same").etag != CachedResponse.of(b"other").etag
