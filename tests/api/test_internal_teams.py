"""
API tests for /v1/internal/teams

Covers:
- GET /v1/internal/teams/ returns user's teams
- User sync (get_or_create_user) is called with the fake user's clerk_user_id and email
- Unauthenticated requests are rejected (see test_auth.py for negative tests)
"""

import pytest
from unittest.mock import MagicMock

from schemas.common import ApiStatus
from schemas.team import TeamGetResp


def _make_fake_user_model(user_id: int = 42) -> MagicMock:
    user = MagicMock()
    user.user_id = user_id
    return user


FAKE_TEAMS_RESP = TeamGetResp(status=ApiStatus.SUCCESS, message="Teams fetched", data=[])


@pytest.mark.api
def test_get_teams_returns_200(authed_client, monkeypatch):
    from services import user_sync_service, team_service

    monkeypatch.setattr(
        user_sync_service.UserSyncService,
        "get_or_create_user",
        staticmethod(lambda clerk_id, email: _make_fake_user_model()),
    )
    async def fake_get_teams(user_id):
        return FAKE_TEAMS_RESP

    monkeypatch.setattr(team_service.TeamService, "get_teams", staticmethod(fake_get_teams))

    res = authed_client.get("/v1/internal/teams/")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["data"] == []


@pytest.mark.api
def test_get_teams_passes_user_id_to_service(authed_client, monkeypatch):
    from services import user_sync_service, team_service

    captured = {}
    fake_user = _make_fake_user_model(user_id=99)

    monkeypatch.setattr(
        user_sync_service.UserSyncService,
        "get_or_create_user",
        staticmethod(lambda clerk_id, email: fake_user),
    )

    async def fake_get_teams(user_id):
        captured["user_id"] = user_id
        return FAKE_TEAMS_RESP

    monkeypatch.setattr(team_service.TeamService, "get_teams", staticmethod(fake_get_teams))

    authed_client.get("/v1/internal/teams/")
    assert captured["user_id"] == 99


@pytest.mark.api
def test_get_teams_syncs_clerk_user(authed_client, monkeypatch):
    """Verify the route passes clerk_user_id and email to UserSyncService."""
    from services import user_sync_service, team_service
    from tests.api.conftest import FAKE_USER

    sync_calls = []

    def fake_sync(clerk_id, email):
        sync_calls.append({"clerk_id": clerk_id, "email": email})
        return _make_fake_user_model()

    async def fake_get_teams(user_id):
        return FAKE_TEAMS_RESP

    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(fake_sync))
    monkeypatch.setattr(team_service.TeamService, "get_teams", staticmethod(fake_get_teams))

    authed_client.get("/v1/internal/teams/")
    assert len(sync_calls) == 1
    assert sync_calls[0]["clerk_id"] == FAKE_USER["clerk_user_id"]
    assert sync_calls[0]["email"] == FAKE_USER["email"]


@pytest.mark.api
def test_add_team_response_exposes_team_id_and_already_exists(authed_client, monkeypatch):
    """TeamAddResp used to drop these kwargs (pydantic extra=ignore); the frontend reads them."""
    from services import user_sync_service, team_service
    from schemas.team import TeamAddResp

    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: _make_fake_user_model()))

    async def fake_add(user_id, league_info):
        return TeamAddResp(status=ApiStatus.SUCCESS, message="Team already exists", team_id=5, already_exists=True)

    monkeypatch.setattr(team_service.TeamService, "add_team", staticmethod(fake_add))
    res = authed_client.post("/v1/internal/teams/add", json={"league_info": {
        "provider": "espn", "league_id": 1, "team_name": "T", "year": 2026, "espn_s2": "x", "swid": "y"}})
    assert res.status_code == 200
    body = res.json()
    assert body["team_id"] == 5 and body["already_exists"] is True
