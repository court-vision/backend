"""Public/private API boundaries for the SQLMate proxy."""

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import make_test_app


def upstream(status: int, body) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "http://sqlmate.railway.internal"),
    )


@pytest.mark.api
def test_schema_is_public_and_passes_through(monkeypatch):
    from services import sqlmate_client

    calls = []

    async def fake_proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return upstream(200, [{"table_name": "players"}])

    monkeypatch.setattr(sqlmate_client, "proxy_request", fake_proxy)
    app = make_test_app()
    app.dependency_overrides.clear()

    response = TestClient(app).get("/v1/sqlmate/schema")

    assert response.status_code == 200
    assert response.json() == [{"table_name": "players"}]
    assert calls == [("GET", "/schema", {"authorization": None})]


@pytest.mark.api
def test_query_is_public_and_forwards_the_exact_payload(monkeypatch):
    from services import sqlmate_client

    calls = []

    async def fake_proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return upstream(200, {"status": {"status": "success"}, "table": None})

    monkeypatch.setattr(sqlmate_client, "proxy_request", fake_proxy)
    app = make_test_app()
    app.dependency_overrides.clear()
    payload = {"query_params": [{"table": "players", "columns": ["name"]}]}

    response = TestClient(app).post("/v1/sqlmate/query", json=payload)

    assert response.status_code == 200
    assert calls == [("POST", "/query", {"json": payload, "authorization": None})]


@pytest.mark.api
def test_saved_table_route_is_internal_and_forwards_verified_bearer(authed_client, monkeypatch):
    from services import sqlmate_client

    calls = []

    async def fake_proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return upstream(200, {"details": {"status": "success"}, "tables": []})

    monkeypatch.setattr(sqlmate_client, "proxy_request", fake_proxy)

    response = authed_client.get("/v1/internal/sqlmate/users/get_tables")

    assert response.status_code == 200
    assert calls == [(
        "GET",
        "/users/get_tables",
        {"authorization": "Bearer fake-jwt-token"},
    )]
