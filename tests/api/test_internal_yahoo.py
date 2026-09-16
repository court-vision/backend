"""The Yahoo OAuth callback stores the grant under the account it belongs to,
and the provider-scoped data routes that took raw credentials are gone."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.v1.internal import yahoo
from services import credential_service
from services.user_sync_service import UserSyncService
from services.yahoo_service import YahooService

TOKENS = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "expires_in": 3600,
    "token_expiry": "2026-09-15T13:00:00",
    "guid": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "scope": "fspt-r",
}


@pytest.fixture
def stored(monkeypatch):
    """The callback's collaborators, with the store's arguments captured."""
    calls: list[dict] = []

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    def fake_store(user_id, provider, secrets, *, account="", scope=None):
        calls.append({"user_id": user_id, "provider": provider, "secrets": secrets,
                      "account": account, "scope": scope})
        return 99

    monkeypatch.setattr(yahoo, "run_db", direct_run_db)
    monkeypatch.setattr(YahooService, "validate_state", staticmethod(lambda state: {"user_id": "user_test_123"}))
    monkeypatch.setattr(YahooService, "exchange_code_for_tokens", AsyncMock(return_value=dict(TOKENS)))
    monkeypatch.setattr(UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id: SimpleNamespace(user_id=7)))
    monkeypatch.setattr(credential_service, "store_provider_tokens", fake_store)
    return calls


@pytest.mark.api
def test_callback_keys_the_row_by_the_guid_and_records_the_scope(client, stored):
    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert res.headers["location"].endswith("/manage-teams?yahoo_connected=true&yahoo_connection=99")
    assert stored == [{
        "user_id": 7,
        "provider": "yahoo",
        "secrets": {"yahoo_access_token": "at-1", "yahoo_refresh_token": "rt-1",
                    "yahoo_token_expiry": "2026-09-15T13:00:00"},
        "account": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "scope": "fspt-r",
    }]


@pytest.mark.api
def test_callback_without_a_guid_stores_an_unkeyed_row(client, stored, monkeypatch):
    monkeypatch.setattr(YahooService, "exchange_code_for_tokens",
                        AsyncMock(return_value={**TOKENS, "guid": ""}))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert stored[0]["account"] == ""


@pytest.mark.api
@pytest.mark.parametrize("path", [
    "/v1/internal/yahoo/validate_league",
    "/v1/internal/yahoo/get_roster_data",
    "/v1/internal/yahoo/get_freeagent_data",
    "/v1/internal/espn/validate_league",
    "/v1/internal/espn/get_roster_data",
    "/v1/internal/espn/get_freeagent_data",
])
def test_the_raw_credential_data_routes_are_gone(authed_client, path):
    """No client called them (the team-scoped routes replaced them), and the
    Yahoo validate route was reachable without signing in."""
    res = authed_client.post(path, json={})
    assert res.status_code == 404
