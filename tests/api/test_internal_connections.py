"""
API tests for /v1/internal/connections — routing, request validation and user
scoping. The service is stubbed; tests/unit/test_espn_connections.py and the
integration suite cover what it does.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from schemas.common import ApiStatus
from schemas.connections import (
    EspnAccountTeam,
    EspnAccountTeamsResp,
    ProviderConnectionDeleteData,
    ProviderConnectionDeleteResp,
    ProviderConnectionInfo,
    ProviderConnectionListResp,
    ProviderConnectionResp,
)

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
VIEW = ProviderConnectionInfo(id=21, provider="espn", account_hint="…E5F6", status="ok",
                              created_at=NOW, updated_at=NOW)


@pytest.fixture
def calls(monkeypatch):
    from services import user_sync_service
    from services.connection_service import ConnectionService

    user = MagicMock()
    user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: user))
    seen = []

    async def list_for_user(user_id):
        seen.append(("list", user_id))
        return ProviderConnectionListResp(status=ApiStatus.SUCCESS, message="Found 1 connections", data=[VIEW])

    async def connect_espn(user_id, espn_s2, swid):
        seen.append(("connect", user_id, espn_s2, swid))
        return ProviderConnectionResp(status=ApiStatus.SUCCESS, message="ESPN account connected",
                                      data=VIEW, created=True)

    async def verify(user_id, connection_id):
        seen.append(("verify", user_id, connection_id))
        return ProviderConnectionResp(status=ApiStatus.SUCCESS, message="ESPN accepted these cookies", data=VIEW)

    async def espn_teams(user_id, connection_id):
        seen.append(("espn_teams", user_id, connection_id))
        team = EspnAccountTeam(league_id=1111111, season=2027, espn_team_id=1, team_name="Alpha",
                               tracked_team_id=7)
        return EspnAccountTeamsResp(status=ApiStatus.SUCCESS, message="Found 1 ESPN teams", data=[team])

    async def delete(user_id, connection_id):
        seen.append(("delete", user_id, connection_id))
        return ProviderConnectionDeleteResp(
            status=ApiStatus.SUCCESS, message="Connection removed",
            data=ProviderConnectionDeleteData(id=connection_id, unlinked_team_ids=[5]),
        )

    for name, fn in (("list_for_user", list_for_user), ("connect_espn", connect_espn),
                     ("verify", verify), ("espn_teams", espn_teams), ("delete", delete)):
        monkeypatch.setattr(ConnectionService, name, staticmethod(fn))
    return seen


@pytest.mark.api
def test_list_is_scoped_to_the_caller(authed_client, calls):
    res = authed_client.get("/v1/internal/connections/")
    assert res.status_code == 200
    item = res.json()["data"][0]
    assert calls == [("list", 42)]
    assert item["account_hint"] == "…E5F6" and item["status"] == "ok" and item["teams"] == []


@pytest.mark.api
def test_connect_passes_the_pasted_pair(authed_client, calls):
    res = authed_client.post("/v1/internal/connections/espn", json={"espn_s2": "AEB", "swid": "{X}"})
    assert res.status_code == 200 and res.json()["created"] is True
    assert calls == [("connect", 42, "AEB", "{X}")]


@pytest.mark.api
@pytest.mark.parametrize("body", [{}, {"espn_s2": "AEB"}, {"espn_s2": "", "swid": "{X}"}])
def test_connect_requires_both_cookies(authed_client, calls, body):
    assert authed_client.post("/v1/internal/connections/espn", json=body).status_code == 422
    assert not calls


@pytest.mark.api
def test_verify_and_delete_route_by_id(authed_client, calls):
    assert authed_client.post("/v1/internal/connections/21/verify").status_code == 200
    res = authed_client.delete("/v1/internal/connections/21")
    assert res.status_code == 200 and res.json()["data"]["unlinked_team_ids"] == [5]
    assert calls == [("verify", 42, 21), ("delete", 42, 21)]


@pytest.mark.api
def test_espn_teams_route_by_id(authed_client, calls):
    res = authed_client.get("/v1/internal/connections/21/espn/teams")
    assert res.status_code == 200
    assert res.json()["data"][0]["tracked_team_id"] == 7
    assert calls == [("espn_teams", 42, 21)]
