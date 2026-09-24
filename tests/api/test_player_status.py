"""HTTP contract for GET /v1/players/{id}/status: only a current report is a status."""

from datetime import date
from types import SimpleNamespace

import pytest

from db.models.nba.player_injuries import PlayerInjury
from services import player_service

pytestmark = pytest.mark.api

TODAY = date(2026, 9, 21)


@pytest.fixture
def current_report(monkeypatch):
    """Serve the route without Postgres; `state["report"]` is the model's answer."""
    from db import base

    state = {"report": None}

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(base, "run_db", direct_run_db)
    monkeypatch.setattr(player_service, "nba_date_et", lambda: TODAY)
    monkeypatch.setattr(
        PlayerInjury,
        "get_current_status",
        staticmethod(lambda player_id, as_of=None: state["report"]),
    )
    return state


def test_current_report_is_served_with_its_date_and_age(client, current_report):
    current_report["report"] = SimpleNamespace(
        status="Out",
        injury_type="Ankle",
        injury_detail="Sprain",
        expected_return=date(2026, 10, 1),
        report_date=date(2026, 9, 20),
    )

    result = client.get("/v1/players/1630578/status")

    assert result.status_code == 200
    assert result.json()["data"] == {
        "status": "Out",
        "injury_type": "Ankle",
        "injury_detail": "Sprain",
        "expected_return": "2026-10-01",
        "report_date": "2026-09-20",
        "report_age_days": 1,
    }


def test_no_current_report_is_null_data_not_an_error(client, current_report):
    result = client.get("/v1/players/1630578/status")

    assert result.status_code == 200
    body = result.json()
    assert body["data"] is None
    assert body["message"] == "No current injury report"
