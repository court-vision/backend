"""
The lifespan's warm-up of the request path (core/warmup.py).

The first requests after every deploy used to pay, between them, for Clerk's
JWKS (fetched by each of 9 parallel requests, ~330 ms each in prod), an empty
DB pool (serial connects) and the OpenAPI schema (692 ms, built on the event
loop by the first /openapi.json while every other request waited). Startup now
pays once. None of it may fail a startup or hold it up for long.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI

import main
from core import clerk_auth, warmup


@pytest.fixture
def parts(monkeypatch):
    """warm_request_path with its JWKS fetch and pool warm-up replaced by recorders."""
    state = SimpleNamespace(jwks=0, pool=[], jwks_delay=0.0, pool_error=None)

    def warm_jwks():
        time.sleep(state.jwks_delay)
        state.jwks += 1

    async def warm_pool(count):
        state.pool.append(count)
        if state.pool_error:
            raise state.pool_error

    monkeypatch.setattr(warmup, "warm_clerk_jwks", warm_jwks)
    monkeypatch.setattr(warmup, "warm_db_pool", warm_pool)
    return state


def _small_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/probe")
    async def probe():
        return {"ok": True}
    return app


@pytest.mark.unit
def test_startup_fetches_jwks_opens_the_pool_and_builds_the_schema(parts, monkeypatch):
    monkeypatch.setattr(warmup.settings, "db_pool_warm_connections", 5)
    app = _small_app()
    assert app.openapi_schema is None

    asyncio.run(warmup.warm_request_path(app))

    assert parts.jwks == 1
    assert parts.pool == [5]
    assert "/v1/probe" in app.openapi_schema["paths"]   # cached: /openapi.json serves it as is


@pytest.mark.unit
def test_the_schema_build_does_not_wait_for_the_jwks_fetch(parts, monkeypatch):
    """The fetch is a network wait in a thread; the schema is CPU on the loop."""
    built = []
    jwks_running = threading.Event()

    def slow_jwks():
        jwks_running.set()
        time.sleep(0.2)
        parts.jwks += 1

    def build(app):
        built.append((jwks_running.wait(1.0), parts.jwks))
    monkeypatch.setattr(warmup, "warm_clerk_jwks", slow_jwks)
    monkeypatch.setattr(warmup, "warm_openapi", build)

    asyncio.run(warmup.warm_request_path(_small_app()))
    assert built == [(True, 0)]   # built while the fetch was still in flight
    assert parts.jwks == 1


@pytest.mark.unit
def test_a_slow_jwks_fetch_does_not_hold_up_startup(parts, monkeypatch):
    monkeypatch.setattr(warmup, "JWKS_WAIT_S", 0.05)
    parts.jwks_delay = 0.5

    async def run():
        started = time.perf_counter()
        await warmup.warm_request_path(_small_app())
        return time.perf_counter() - started
    assert asyncio.run(run()) < 0.4


@pytest.mark.unit
def test_failures_do_not_fail_startup(parts, monkeypatch):
    parts.pool_error = RuntimeError("pool exploded")
    app = _small_app()

    def broken_openapi():
        raise ValueError("bad schema")
    monkeypatch.setattr(app, "openapi", broken_openapi)

    asyncio.run(warmup.warm_request_path(app))   # does not raise
    assert parts.jwks == 1


@pytest.mark.unit
@pytest.mark.parametrize("body", [b"null", b"[]", b'"keys"'])
def test_a_jwks_answer_that_is_not_a_key_set_does_not_fail_startup(parts, monkeypatch, body):
    """Clerk answering 200 with JSON that is not a key set once stopped the API starting at all."""
    response = requests.Response()
    response.status_code = 200
    response._content = body
    monkeypatch.setattr(clerk_auth._clerk_http, "get", lambda url, timeout=None: response)
    monkeypatch.setattr(warmup, "warm_clerk_jwks", clerk_auth.warm_clerk_jwks)   # the real one
    clerk_auth.clear_jwks_cache()
    app = _small_app()
    try:
        asyncio.run(warmup.warm_request_path(app))   # does not raise
        assert clerk_auth._jwks is None   # nothing malformed was cached
    finally:
        clerk_auth.clear_jwks_cache()
    assert app.openapi_schema is not None


@pytest.mark.unit
def test_a_jwks_warm_up_that_raises_does_not_fail_startup(parts, monkeypatch):
    def broken():
        raise AttributeError("'NoneType' object has no attribute 'get'")
    monkeypatch.setattr(warmup, "warm_clerk_jwks", broken)
    app = _small_app()

    asyncio.run(warmup.warm_request_path(app))   # does not raise
    assert app.openapi_schema is not None


@pytest.mark.unit
def test_the_real_app_schema_is_built(parts):
    """Every router is included at import, before the lifespan runs."""
    main.app.openapi_schema = None
    try:
        asyncio.run(warmup.warm_request_path(main.app))
        paths = main.app.openapi_schema["paths"]
        assert "/v1/internal/teams/" in paths
        assert "/ping" in paths
    finally:
        main.app.openapi_schema = None


@pytest.mark.unit
def test_lifespan_warms_the_request_path_before_serving(monkeypatch):
    """main.lifespan runs the warm-up during startup, with the real app."""
    calls = []

    async def stopped():
        return None
    for name in ("setup_logging", "init_db", "start_db_runtime", "start_cpu_runtime", "start_provider_runtime",
                 "start_blocking_provider_runtime", "start_features_runtime", "start_sqlmate_runtime",
                 "start_fantasy_writer_runtime", "assert_calendar_available", "close_db"):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: None)
    for name in ("stop_features_runtime", "stop_sqlmate_runtime", "stop_fantasy_writer_runtime",
                 "stop_provider_runtime", "stop_blocking_provider_runtime", "stop_cpu_runtime", "stop_db_runtime"):
        monkeypatch.setattr(main, name, stopped)
    monkeypatch.setattr(main, "start_loop_watchdog", lambda loop, stall: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(main.ai_questions, "keep_redacting", stopped)

    async def warm(app):
        calls.append(app)
    monkeypatch.setattr(main, "warm_request_path", warm)

    async def run():
        async with main.lifespan(main.app):
            calls.append("serving")
    asyncio.run(run())
    assert calls == [main.app, "serving"]
