"""
core/tracing.py and services/request_timings.py end to end: a request through
`RequestSpanMiddleware` comes out of the writer as one ops.request_timings row
with its route, status and buckets.

These use the process-wide tracer provider, as the app does: `init_tracing` is
idempotent, so whichever of main.py or this module installs it first, finished
requests land on `request_timings.queue`.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import tracing
from core.correlation_middleware import RequestContextMiddleware
from core.middleware import setup_middleware
from core.request_timing import BUCKET_ATTR, summarize
from core.settings import settings
from core.tracing import RequestSpanMiddleware, timed, trace_headers, waiting
from db.base import db
from services import request_timings

TRACE = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture
def finished():
    """The requests finished during the test, as (server span, spans)."""
    tracing.init_tracing(request_timings.queue, service_name="api", version="test", database=db)
    request_timings.queue.take(10**6)
    taken = []

    def take():
        taken.extend(request_timings.queue.take(10**6))
        return taken

    yield take
    request_timings.queue.take(10**6)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    setup_middleware(app)
    app.add_middleware(RequestSpanMiddleware)
    seen = {}

    @app.get("/v1/things/{thing_id}")
    async def get_thing(thing_id: int):
        with timed("espn", "GET espn"):
            with waiting("espn slot"):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
        seen["headers"] = trace_headers({"X-Correlation-ID": "abc"})
        return {"id": thing_id}

    @app.get("/v1/boom")
    async def boom():
        raise RuntimeError("unhandled")

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.state.seen = seen
    return app


def _rows(requests, *, keep_trace_id=False):
    return [summarize(server, spans, service="api", version="test", keep_trace_id=keep_trace_id)
            for server, spans in requests]


@pytest.mark.unit
def test_a_request_becomes_one_row_with_its_route_and_buckets(finished):
    app = _app()
    response = TestClient(app).get("/v1/things/7")

    assert response.status_code == 200
    [row] = _rows(finished())
    assert (row["method"], row["route"], row["status_code"]) == ("GET", "/v1/things/{thing_id}", 200)
    assert row["calls"] == {"espn": 1}
    assert row["parts"]["espn"] >= 25 and row["parts"]["wait"] >= 8
    assert sum(row["parts"].values()) == pytest.approx(row["duration_ms"], abs=0.5)
    # Our own services get the request's trace; a provider never does.
    assert app.state.seen["headers"]["X-Correlation-ID"] == "abc"
    assert app.state.seen["headers"]["traceparent"].startswith("00-")


@pytest.mark.unit
def test_an_unhandled_error_is_a_500_row(finished):
    response = TestClient(_app(), raise_server_exceptions=False).get("/v1/boom")

    assert response.status_code == 500
    [row] = _rows(finished())
    assert (row["route"], row["status_code"]) == ("/v1/boom", 500)


@pytest.mark.unit
def test_health_polls_preflights_and_unmatched_paths_have_no_row(finished):
    client = TestClient(_app())
    client.get("/health")
    client.options("/v1/things/7", headers={"Origin": "https://courtvision.dev",
                                            "Access-Control-Request-Method": "GET"})
    assert client.get("/wp-login.php").status_code == 404

    assert [row for row in _rows(finished()) if row is not None] == []


@pytest.mark.unit
def test_the_edge_traceparent_is_continued_and_kept_only_when_sampled(finished):
    client = TestClient(_app())
    client.get("/v1/things/1", headers={"traceparent": f"00-{TRACE}-b7ad6b7169203331-01"})
    client.get("/v1/things/2", headers={"traceparent": f"00-{TRACE}-b7ad6b7169203331-00"})

    sampled, unsampled = _rows(finished(), keep_trace_id=True)
    assert sampled["trace_id"] == TRACE
    assert unsampled["trace_id"] is None and unsampled["calls"] == {"espn": 1}


@pytest.mark.unit
def test_each_query_is_a_postgres_span_with_its_statement_and_no_values(finished):
    class FakeDatabase:
        def execute_sql(self, sql, params=None):
            return ("ran", sql, params)

    database = FakeDatabase()
    tracing._trace_queries(database)
    app = FastAPI()
    app.add_middleware(RequestSpanMiddleware)

    @app.get("/v1/query")
    def query():
        assert database.execute_sql('select "id" from "nba"."players" where "id" = %s', (5,))[0] == "ran"
        return {}

    TestClient(app).get("/v1/query")

    [(server, spans)] = finished()
    [span] = spans
    assert span.name == "SELECT" and span.attributes[BUCKET_ATTR] == "postgres"
    assert span.attributes["db.query.text"] == 'select "id" from "nba"."players" where "id" = %s'
    assert "5" not in str(dict(span.attributes))
    assert _rows([(server, spans)])[0]["calls"] == {"postgres": 1}


# ---- the writer ----

@pytest.mark.unit
@pytest.mark.asyncio
async def test_flush_writes_one_row_per_routed_request(finished, monkeypatch):
    import db.base

    async def direct(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    inserted = []

    class Insert:
        def __init__(self, rows):
            self.rows = rows

        def execute(self):
            inserted.extend(self.rows)

    monkeypatch.setattr(db.base, "run_db", direct)
    monkeypatch.setattr(request_timings.RequestTiming, "insert_many", Insert)
    client = TestClient(_app())
    client.get("/v1/things/3")
    client.get("/nowhere")

    assert await request_timings.flush() == 1
    [row] = inserted
    assert row["route"] == "/v1/things/{thing_id}" and row["service"] == "api"
    assert row["version"] == settings.version
    assert len(request_timings.queue) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flush_reports_requests_the_full_queue_dropped(monkeypatch):
    queue = request_timings.RequestQueue(maxlen=0)
    monkeypatch.setattr(request_timings, "queue", queue)
    queue(object(), [])

    assert await request_timings.flush() == 0
    assert queue.take_dropped() == 0  # taken, and logged, by the flush


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prune_deletes_rows_past_the_retention(monkeypatch):
    cutoffs = []

    async def fake_prune(cutoff):
        cutoffs.append(cutoff)
        return 3

    monkeypatch.setattr(request_timings, "_prune", fake_prune)

    assert await request_timings.prune() == 3
    expected = datetime.now(timezone.utc) - timedelta(days=settings.request_timings_retention_days)
    assert abs(cutoffs[0] - expected) < timedelta(seconds=5)
