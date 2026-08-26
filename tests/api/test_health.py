"""
`GET /health` on the real app: 200 `ok` with the contract's shape when the
database answers, 503 `degraded` when it does not. `/ping` stays static.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from core import health as health_module
from main import app as main_app


@pytest.fixture
def client():
    return TestClient(main_app, raise_server_exceptions=False)


def _fake_database_check(result):
    async def check(timeout=2.0):
        return result
    return check


@pytest.mark.api
def test_health_ok_shape(client, monkeypatch):
    monkeypatch.setattr(health_module, "database_check", _fake_database_check({"ok": True, "latency_ms": 1.2}))

    res = client.get("/health", headers={"X-Correlation-ID": "t-health"})

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "court-vision-api"
    assert body["version"]  # "dev" locally, the short SHA on Railway
    assert body["environment"] == "development"
    assert isinstance(body["uptime_s"], int) and body["uptime_s"] >= 0
    assert body["checks"]["database"] == {"ok": True, "latency_ms": 1.2}
    assert body["checks"]["calendar"]["season"] == "2025-26"
    assert "ok" in body["checks"]["calendar"]
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["X-Correlation-ID"] == "t-health"


@pytest.mark.api
def test_health_degraded_when_database_fails(client, monkeypatch):
    monkeypatch.setattr(health_module, "database_check", _fake_database_check({"ok": False, "error": "OperationalError"}))
    monkeypatch.setattr(health_module, "_last_degraded_log_at", 0.0)

    res = client.get("/health")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == {"ok": False, "error": "OperationalError"}
    assert body["checks"]["calendar"]["season"] == "2025-26"


@pytest.mark.api
def test_calendar_does_not_gate_health(client, monkeypatch):
    monkeypatch.setattr(health_module, "database_check", _fake_database_check({"ok": True, "latency_ms": 0.5}))
    monkeypatch.setattr(health_module, "calendar_check", lambda: {"ok": False, "season": "2025-26", "error": "FileNotFoundError"})

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json()["checks"]["calendar"]["ok"] is False


@pytest.mark.api
def test_ping_is_static(client):
    res = client.get("/ping")
    assert res.status_code == 200
    assert res.json() == {"message": "Pong!"}


@pytest.mark.api
def test_database_check_times_out(monkeypatch):
    def slow_probe():
        time.sleep(0.3)
        return 1.0

    monkeypatch.setattr(health_module, "_probe_database", slow_probe)
    result = asyncio.run(health_module.database_check(timeout=0.02))
    assert result["ok"] is False
    assert "timeout" in result["error"]


@pytest.mark.api
def test_database_check_reports_error_type_only(monkeypatch):
    from peewee import OperationalError

    def failing_probe():
        raise OperationalError('could not connect to server: password "hunter2" rejected')

    monkeypatch.setattr(health_module, "_probe_database", failing_probe)
    result = asyncio.run(health_module.database_check(timeout=1.0))
    assert result == {"ok": False, "error": "OperationalError"}
