"""InFlight: concurrent identical calls share one execution; nothing outlives it.

The provider layer's own tests (test_provider_http.py) cover what makes two
GETs the same read; these pin the mechanism: one execution, independent of
which caller started it, cancelled only when nobody is left, and gone the
moment it settles.
"""

import asyncio

import pytest

from core import inflight
from core.inflight import InFlight, provider_write, write_epoch


async def _until(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


class _Upstream:
    """A call that blocks until released, counting how often it actually ran.
    It keeps the `seal` it was handed, so a test can close it to new callers."""

    def __init__(self):
        self.runs = 0
        self.cancelled = 0
        self.release = asyncio.Event()
        self.error = None
        self.seal = None

    async def __call__(self, seal):
        self.seal = seal
        self.runs += 1
        run = self.runs
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error is not None:
            raise self.error
        return {"run": run}


@pytest.mark.unit
def test_concurrent_calls_with_one_key_run_once_and_share_the_result():
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        tasks = [asyncio.create_task(flights.run("k", upstream)) for _ in range(5)]
        await _until(lambda: upstream.runs == 1)
        await _settle()
        upstream.release.set()
        return upstream, await asyncio.gather(*tasks)

    upstream, results = asyncio.run(scenario())
    assert upstream.runs == 1
    assert results == [{"run": 1}] * 5
    assert (flights.started, flights.coalesced) == (1, 4)
    assert flights.stats()["in_flight"] == 0


@pytest.mark.unit
def test_different_keys_run_separately():
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        tasks = [asyncio.create_task(flights.run(key, upstream)) for key in ("a", "b", "a")]
        await _until(lambda: upstream.runs == 2)
        upstream.release.set()
        return upstream, await asyncio.gather(*tasks)

    upstream, results = asyncio.run(scenario())
    assert upstream.runs == 2
    assert results[0] == results[2] != results[1]


@pytest.mark.unit
def test_sequential_calls_are_not_cached():
    """In flight only: a call made after another finished runs again."""
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        upstream.release.set()
        first = await flights.run("k", upstream)
        second = await flights.run("k", upstream)
        return upstream, first, second

    upstream, first, second = asyncio.run(scenario())
    assert upstream.runs == 2
    assert (first, second) == ({"run": 1}, {"run": 2})
    assert flights.coalesced == 0


@pytest.mark.unit
def test_a_failure_reaches_every_waiter_and_frees_the_key():
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        upstream.error = RuntimeError("upstream down")
        tasks = [asyncio.create_task(flights.run("k", upstream)) for _ in range(3)]
        await _until(lambda: upstream.runs == 1)
        await _settle()
        upstream.release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        upstream.error = None
        after = await flights.run("k", upstream)
        return upstream, outcomes, after

    upstream, outcomes, after = asyncio.run(scenario())
    assert all(isinstance(o, RuntimeError) and str(o) == "upstream down" for o in outcomes)
    assert outcomes[0] is outcomes[1] is outcomes[2]     # the same exception, not three
    assert after == {"run": 2} and upstream.runs == 2


@pytest.mark.unit
def test_the_first_caller_giving_up_does_not_cancel_the_shared_call():
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        leader = asyncio.create_task(flights.run("k", upstream))
        await _until(lambda: upstream.runs == 1)
        followers = [asyncio.create_task(flights.run("k", upstream)) for _ in range(2)]
        await _settle()
        leader.cancel()
        await _settle()
        upstream.release.set()
        results = await asyncio.gather(*followers)
        with pytest.raises(asyncio.CancelledError):
            await leader
        return upstream, results

    upstream, results = asyncio.run(scenario())
    assert results == [{"run": 1}, {"run": 1}]
    assert upstream.runs == 1 and upstream.cancelled == 0


@pytest.mark.unit
def test_when_every_caller_gives_up_the_call_is_cancelled_and_the_key_is_free():
    """A lone caller's cancellation still reaches the call, as it did before
    coalescing; and a caller arriving afterwards starts afresh rather than
    joining the cancellation."""
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        tasks = [asyncio.create_task(flights.run("k", upstream)) for _ in range(2)]
        await _until(lambda: upstream.runs == 1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _until(lambda: upstream.cancelled == 1)
        assert flights.stats()["in_flight"] == 0
        upstream.release.set()
        return upstream, await flights.run("k", upstream)

    upstream, after = asyncio.run(scenario())
    assert upstream.cancelled == 1
    assert after == {"run": 2}


@pytest.mark.unit
def test_a_call_cancelled_before_it_ever_ran_leaves_nothing_behind():
    """A task cancelled before its first step never enters its own `finally`
    (loop shutdown does this); the done callback still clears the key."""
    flights = InFlight("test")

    async def scenario():
        upstream = _Upstream()
        upstream.release.set()
        caller = asyncio.create_task(flights.run("k", upstream))
        await asyncio.sleep(0)       # run() has created the call's task, which has not run yet
        flights._flights["k"].task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await _settle()
        assert flights.stats()["in_flight"] == 0
        assert upstream.runs == 0
        return upstream, await flights.run("k", upstream)

    upstream, after = asyncio.run(scenario())
    assert upstream.runs == 1 and after == {"run": 1}


@pytest.mark.unit
def test_a_sealed_call_answers_its_waiters_but_takes_no_new_ones():
    """The provider layer seals a read before retrying it: callers already
    waiting share the retry, a caller arriving after the seal starts a call of
    its own, and the sealed call landing leaves the new one in place."""
    flights = InFlight("test")

    async def scenario():
        first, second = _Upstream(), _Upstream()
        early = [asyncio.create_task(flights.run("k", first)) for _ in range(2)]
        await _until(lambda: first.runs == 1)
        await _settle()
        first.seal()
        assert flights.stats()["in_flight"] == 0
        late = [asyncio.create_task(flights.run("k", second)) for _ in range(2)]
        await _until(lambda: second.runs == 1)
        await _settle()
        first.release.set()
        early_results = await asyncio.gather(*early)
        assert flights.stats()["in_flight"] == 1      # the sealed call landing did not evict the new one
        latest = asyncio.create_task(flights.run("k", second))
        await _settle()
        second.release.set()
        return first, second, early_results, await asyncio.gather(*late, latest)

    first, second, early_results, late_results = asyncio.run(scenario())
    assert early_results == [{"run": 1}] * 2
    assert late_results == [{"run": 1}] * 3
    assert first.runs == second.runs == 1
    assert (flights.started, flights.coalesced) == (2, 3)
    assert flights.stats()["in_flight"] == 0


@pytest.mark.unit
def test_cancelling_a_sealed_call_leaves_the_new_one_alone():
    """The last waiter of a sealed call giving up cancels that call only."""
    flights = InFlight("test")

    async def scenario():
        first, second = _Upstream(), _Upstream()
        old = asyncio.create_task(flights.run("k", first))
        await _until(lambda: first.runs == 1)
        first.seal()
        new = asyncio.create_task(flights.run("k", second))
        await _until(lambda: second.runs == 1)
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        await _until(lambda: first.cancelled == 1)
        assert flights.stats()["in_flight"] == 1
        second.release.set()
        return second, await new

    second, result = asyncio.run(scenario())
    assert result == {"run": 1} and second.cancelled == 0


@pytest.mark.unit
def test_a_flight_stranded_on_another_loop_is_replaced_not_joined():
    """The registry is module state and outlives an event loop (each test gets
    its own). A call stranded on a loop that is no longer running can never
    finish on this one, so joining it would hang; a caller here starts afresh."""
    flights = InFlight("test")
    old_loop = asyncio.new_event_loop()
    stranded = _Upstream()
    caller = old_loop.create_task(flights.run("k", stranded))
    old_loop.run_until_complete(_until(lambda: stranded.runs == 1))
    assert flights.stats()["in_flight"] == 1          # mid-flight, and its loop has stopped

    async def scenario():
        upstream = _Upstream()
        upstream.release.set()
        return upstream, await asyncio.wait_for(flights.run("k", upstream), 1)

    try:
        upstream, result = asyncio.run(scenario())
        assert upstream.runs == 1 and result == {"run": 1}
        assert flights.coalesced == 0
    finally:
        caller.cancel()
        old_loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(old_loop), return_exceptions=True))
        old_loop.close()


@pytest.mark.unit
def test_provider_write_bumps_the_epoch_when_it_starts_and_when_it_ends():
    before = write_epoch()
    with provider_write():
        during = write_epoch()
    assert during == before + 1
    assert write_epoch() == before + 2


@pytest.mark.unit
def test_a_failed_write_still_closes_the_fence():
    before = write_epoch()
    with pytest.raises(RuntimeError):
        with provider_write():
            raise RuntimeError("writer down")
    assert write_epoch() == before + 2


@pytest.mark.unit
def test_epoch_is_process_wide():
    """A module global, not a contextvar: a write in one request fences reads in every other."""
    assert isinstance(inflight._write_epoch, int)

    async def scenario():
        seen = []

        async def write():
            with provider_write():
                await asyncio.sleep(0)

        async def read():
            seen.append(write_epoch())

        before = write_epoch()
        await asyncio.gather(write(), read())
        return before, seen

    before, seen = asyncio.run(scenario())
    assert seen == [before + 1]
