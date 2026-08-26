"""Lineup generation rejects weeks outside the season calendar before calling the features service."""

from unittest.mock import MagicMock

import pytest

from schemas.common import ApiStatus
from schemas.lineup import GenerateLineupResp


@pytest.fixture
def as_user(monkeypatch):
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))


BODY = {"team_id": 7, "streaming_slots": 2, "week": 3, "avg_mode": "season"}


@pytest.mark.api
def test_unknown_week_is_422(authed_client, as_user, monkeypatch):
    from api.v1.internal import lineups
    from services import lineup_service
    monkeypatch.setattr(lineups, "get_matchup_by_number", lambda n: None)

    async def must_not_run(*a, **k):
        raise AssertionError("generate_lineup must not run for an unknown week")

    monkeypatch.setattr(lineup_service.LineupService, "generate_lineup", staticmethod(must_not_run))
    res = authed_client.post("/v1/internal/lineups/generate", json={**BODY, "week": 27})
    assert res.status_code == 422
    assert "27" in res.json()["message"]


@pytest.mark.api
def test_week_above_schema_bound_is_422(authed_client, as_user):
    res = authed_client.post("/v1/internal/lineups/generate", json={**BODY, "week": 99})
    assert res.status_code == 422


@pytest.mark.api
def test_known_week_reaches_the_service(authed_client, as_user, monkeypatch):
    from api.v1.internal import lineups
    from services import lineup_service
    monkeypatch.setattr(lineups, "get_matchup_by_number", lambda n: {"matchup_number": n})
    captured = {}

    async def fake(user_id, team_id, slots, week, avg_mode):
        captured.update(user_id=user_id, team_id=team_id, slots=slots, week=week, avg_mode=avg_mode)
        return GenerateLineupResp(status=ApiStatus.SUCCESS, message="ok", data=None)

    monkeypatch.setattr(lineup_service.LineupService, "generate_lineup", staticmethod(fake))
    res = authed_client.post("/v1/internal/lineups/generate", json={**BODY, "week": 22})
    assert res.status_code == 200
    assert captured == {"user_id": 42, "team_id": 7, "slots": 2, "week": 22, "avg_mode": "season"}
