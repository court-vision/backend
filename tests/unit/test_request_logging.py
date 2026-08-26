"""
`RequestContextMiddleware` logs exactly one `http_request` line per request
with the route template, status, duration, client ip, user agent and error
code — including for 500s, where it cannot touch the response.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from core.correlation_middleware import RequestContextMiddleware
from core.errors import NotFoundError
from core.middleware import setup_middleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    setup_middleware(app)

    @app.get("/v1/things/{thing_id}")
    async def get_thing(thing_id: int):
        return {"id": thing_id}

    @app.get("/v1/me")
    async def me(request: Request):
        request.state.user_id = 42
        return {"ok": True}

    @app.get("/missing")
    async def missing():
        raise NotFoundError("THING_NOT_FOUND", "nope")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/ping")
    async def ping():
        return {"message": "Pong!"}

    return app


@pytest.fixture
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


def _request_lines(logs):
    return [entry for entry in logs if entry["event"] == "http_request"]


@pytest.mark.unit
def test_http_request_line_fields(client):
    with capture_logs() as logs:
        res = client.get(
            "/v1/things/7?x=1",
            headers={
                "X-Correlation-ID": "abc-123",
                "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
                "User-Agent": "ua/1.0",
            },
        )

    assert res.status_code == 200
    lines = _request_lines(logs)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["log_level"] == "info"
    assert entry["method"] == "GET"
    assert entry["path"] == "/v1/things/7"
    assert entry["route"] == "/v1/things/{thing_id}"
    assert entry["status_code"] == 200
    assert isinstance(entry["duration_ms"], float) and entry["duration_ms"] >= 0
    assert entry["client_ip"] == "203.0.113.9"
    assert entry["user_agent"] == "ua/1.0"
    assert entry["correlation_id"] == "abc-123"
    assert "error_code" not in entry
    assert "user_id" not in entry


@pytest.mark.unit
def test_client_ip_falls_back_to_the_socket(client):
    with capture_logs() as logs:
        client.get("/v1/things/1")
    assert _request_lines(logs)[0]["client_ip"] == "testclient"


@pytest.mark.unit
def test_user_agent_is_truncated(client):
    with capture_logs() as logs:
        client.get("/v1/things/1", headers={"User-Agent": "x" * 500})
    assert len(_request_lines(logs)[0]["user_agent"]) == 120


@pytest.mark.unit
def test_error_code_comes_from_the_response_header(client):
    with capture_logs() as logs:
        res = client.get("/missing")
    assert res.status_code == 404
    entry = _request_lines(logs)[0]
    assert entry["status_code"] == 404
    assert entry["error_code"] == "THING_NOT_FOUND"
    assert entry["route"] == "/missing"


@pytest.mark.unit
def test_unhandled_error_still_logs_a_500_line(client):
    with capture_logs() as logs:
        res = client.get("/boom", headers={"X-Correlation-ID": "c-500"})
    assert res.status_code == 500
    assert res.headers["X-Correlation-ID"] == "c-500"
    entry = _request_lines(logs)[0]
    assert entry["status_code"] == 500
    assert entry["error_code"] == "INTERNAL_ERROR"
    assert entry["correlation_id"] == "c-500"
    # and the handler logged the stack trace under its own event
    assert any(e["event"] == "unhandled_error" and e.get("exc_info") for e in logs)


@pytest.mark.unit
def test_user_id_is_included_when_a_dependency_set_it(client):
    with capture_logs() as logs:
        client.get("/v1/me")
    assert _request_lines(logs)[0]["user_id"] == 42


@pytest.mark.unit
def test_health_style_paths_log_at_debug(client):
    with capture_logs() as logs:
        client.get("/ping")
    entry = _request_lines(logs)[0]
    assert entry["log_level"] == "debug"


@pytest.mark.unit
def test_unknown_route_has_no_template(client):
    with capture_logs() as logs:
        res = client.get("/does-not-exist")
    assert res.status_code == 404
    entry = _request_lines(logs)[0]
    assert entry["route"] is None
    assert entry["error_code"] == "HTTP_404"


@pytest.mark.unit
def test_garbage_correlation_ids_are_replaced(client):
    res = client.get("/v1/things/1", headers={"X-Correlation-ID": "x" * 300})
    assert res.headers["X-Correlation-ID"] != "x" * 300
    assert len(res.headers["X-Correlation-ID"]) == 36
