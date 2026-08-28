"""DB-worker admission, cancellation, context, and event-loop responsiveness."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from core.errors import DatabaseUnavailableError
from core.logging import get_correlation_id, set_correlation_id
from core.settings import settings
from db import base as db_base
from services import features_client


@pytest_asyncio.fixture(autouse=True)
async def isolated_db_runtime(monkeypatch):
    await db_base.stop_db_runtime()
    monkeypatch.setattr(db_base.db, "close_all", lambda: None)
    monkeypatch.setattr(db_base.db, "connection_context", lambda: contextlib.nullcontext())
    yield
    await db_base.stop_db_runtime()
    set_correlation_id("")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_work_is_thread_bound_bounded_and_propagates_context(monkeypatch):
    monkeypatch.setattr(settings, "db_max_in_flight", 4)
    monkeypatch.setattr(settings, "db_queue_timeout_seconds", 1.0)
    db_base.start_db_runtime()

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

    set_correlation_id("corr-db-worker")
    results = await asyncio.gather(*(db_base.run_db("test.bounded", operation) for _ in range(20)))

    assert maximum == 4
    assert all(thread_id != main_thread for thread_id, _ in results)
    assert {correlation_id for _, correlation_id in results} == {"corr-db-worker"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_keeps_capacity_until_worker_finishes(monkeypatch):
    monkeypatch.setattr(settings, "db_max_in_flight", 1)
    monkeypatch.setattr(settings, "db_queue_timeout_seconds", 0.02)
    db_base.start_db_runtime()
    started = threading.Event()
    release = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=1)
        return "done"

    first = asyncio.create_task(db_base.run_db("test.cancelled", blocked))
    assert await asyncio.to_thread(started.wait, 0.5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    with pytest.raises(DatabaseUnavailableError):
        await db_base.run_db("test.must_wait", lambda: None)

    release.set()
    for _ in range(50):
        try:
            assert await db_base.run_db("test.after_release", lambda: "available") == "available"
            break
        except DatabaseUnavailableError:
            await asyncio.sleep(0.005)
    else:
        pytest.fail("DB permit was not released after the cancelled worker completed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ping_remains_responsive_while_db_workers_are_blocked(monkeypatch):
    monkeypatch.setattr(settings, "db_max_in_flight", 4)
    monkeypatch.setattr(settings, "db_queue_timeout_seconds", 1.0)
    db_base.start_db_runtime()

    app = FastAPI()

    @app.get("/slow")
    async def slow():
        await db_base.run_db("test.slow", time.sleep, 0.12)
        return {"ok": True}

    @app.get("/ping")
    async def ping():
        return {"message": "Pong!"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        slow_requests = [asyncio.create_task(client.get("/slow")) for _ in range(12)]
        await asyncio.sleep(0.01)
        latencies = []
        for _ in range(20):
            started_at = time.perf_counter()
            response = await client.get("/ping")
            latencies.append(time.perf_counter() - started_at)
            assert response.status_code == 200
        await asyncio.gather(*slow_requests)

    ordered = sorted(latencies)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    assert p95 < 0.1
    assert max(latencies) < 0.25


@pytest.mark.unit
@pytest.mark.asyncio
async def test_features_client_is_reused_and_capacity_is_bounded(monkeypatch):
    await features_client.stop_features_runtime()
    monkeypatch.setattr(features_client, "client_factory", None)
    monkeypatch.setattr(settings, "features_max_in_flight", 2)
    active = maximum = calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"status": "ok"})
        finally:
            active -= 1

    client = httpx.AsyncClient(
        base_url="http://features.test",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(features_client, "make_client", lambda: client)
    features_client.start_features_runtime()
    try:
        results = await asyncio.gather(*(features_client.get_healthz() for _ in range(8)))
        assert all(result == {"status": "ok"} for result in results)
        assert calls == 8 and maximum == 2
        assert features_client._client is client
    finally:
        await features_client.stop_features_runtime()
    assert client.is_closed
