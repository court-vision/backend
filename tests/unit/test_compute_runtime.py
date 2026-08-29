"""CPU-worker admission: thread-bound, bounded, context-propagating, load-shedding."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from core import compute
from core.errors import ServiceUnavailableError
from core.logging import get_correlation_id, set_correlation_id
from core.settings import settings


@pytest_asyncio.fixture(autouse=True)
async def isolated_cpu_runtime():
    await compute.stop_cpu_runtime()
    yield
    await compute.stop_cpu_runtime()
    set_correlation_id("")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cpu_work_is_thread_bound_bounded_and_propagates_context(monkeypatch):
    monkeypatch.setattr(settings, "cpu_max_in_flight", 2)
    monkeypatch.setattr(settings, "cpu_queue_timeout_seconds", 2.0)
    compute.start_cpu_runtime()

    main_thread = threading.get_ident()
    active = maximum = 0
    lock = threading.Lock()

    def operation():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.02)
            return threading.get_ident(), get_correlation_id()
        finally:
            with lock:
                active -= 1

    set_correlation_id("corr-cpu-worker")
    results = await asyncio.gather(*(compute.run_cpu("test.bounded", operation) for _ in range(10)))

    assert maximum == 2
    assert all(thread_id != main_thread for thread_id, _ in results)
    assert {correlation_id for _, correlation_id in results} == {"corr-cpu-worker"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_work_queued_past_the_timeout_is_shed_as_503(monkeypatch):
    monkeypatch.setattr(settings, "cpu_max_in_flight", 1)
    monkeypatch.setattr(settings, "cpu_queue_timeout_seconds", 0.02)
    compute.start_cpu_runtime()

    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=1)

    occupying = asyncio.create_task(compute.run_cpu("test.blocked", blocked))
    assert await asyncio.to_thread(started.wait, 0.5)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        await compute.run_cpu("test.queued", lambda: None)
    assert excinfo.value.status_code == 503

    release.set()
    await occupying


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_event_loop_stays_responsive_while_cpu_workers_are_busy(monkeypatch):
    """The reason this work is not simply awaited inline on the loop."""
    monkeypatch.setattr(settings, "cpu_max_in_flight", 2)
    monkeypatch.setattr(settings, "cpu_queue_timeout_seconds", 2.0)
    compute.start_cpu_runtime()

    app = FastAPI()

    @app.get("/heavy")
    async def heavy():
        await compute.run_cpu("test.heavy", time.sleep, 0.12)
        return {"ok": True}

    @app.get("/ping")
    async def ping():
        return {"message": "Pong!"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        heavy_requests = [asyncio.create_task(client.get("/heavy")) for _ in range(6)]
        await asyncio.sleep(0.01)
        latencies = []
        for _ in range(20):
            started_at = time.perf_counter()
            assert (await client.get("/ping")).status_code == 200
            latencies.append(time.perf_counter() - started_at)
        await asyncio.gather(*heavy_requests)

    assert max(latencies) < 0.25
