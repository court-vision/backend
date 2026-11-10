"""
GET /v1/internal/teams/{id}/availability over the test app: ownership via the usual
`ensure_team_owned` stub, the verdict read replaced. The route checks the range and the
player count and hands the ids on; it never reads ESPN.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.availability import Verdict
from services import availability_service as svc


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


@pytest.fixture
def reads(monkeypatch):
    calls = []

    def fake_rows(nba_ids, start, end):
        calls.append((list(nba_ids), start, end))
        return [Verdict(nba_player_id=1628973, game_date="2026-11-10", verdict="out", decided_by="espn_injuries",
                        reason="Left Ankle; Sprain", previous_verdict="questionable",
                        changed_at=datetime(2026, 11, 10, 21, 30, tzinfo=timezone.utc))]

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(svc, "rows", fake_rows)
    monkeypatch.setattr(svc, "run_db", direct)
    return calls


@pytest.mark.api
def test_the_verdicts_for_the_players_and_days_named(authed_client, owned, reads):
    r = authed_client.get("/v1/internal/teams/7/availability",
                          params={"start": "2026-11-10", "end": "2026-11-16", "nba_ids": [1628973, 1628404, 1628973]})
    assert r.status_code == 200
    assert reads == [([1628404, 1628973], date(2026, 11, 10), date(2026, 11, 16))]
    body = r.json()["data"]
    assert (body["start"], body["end"]) == ("2026-11-10", "2026-11-16")
    assert body["verdicts"][0] | {"changed_at": None} == {
        "nba_player_id": 1628973, "game_date": "2026-11-10", "verdict": "out", "final": False,
        "decided_by": "espn_injuries", "reason": "Left Ankle; Sprain", "detail": None, "expected_return": None,
        "previous_verdict": "questionable", "changed_at": None}


@pytest.mark.api
@pytest.mark.parametrize("params,code", [
    ({"start": "2026-11-16", "end": "2026-11-10"}, "AVAILABILITY_RANGE_INVALID"),
    ({"start": "2026-11-01", "end": "2026-11-20"}, "AVAILABILITY_RANGE_INVALID"),
    ({"start": "2026-11-10", "end": "2026-11-10", "nba_ids": list(range(1, 62))}, "AVAILABILITY_TOO_MANY_PLAYERS"),
])
def test_a_range_or_a_roster_too_big_is_refused(authed_client, owned, reads, params, code):
    r = authed_client.get("/v1/internal/teams/7/availability", params=params)
    assert r.status_code == 422 and r.json()["error_code"] == code
    assert reads == []
