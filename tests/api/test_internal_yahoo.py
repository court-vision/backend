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
    assert res.headers["location"].endswith("/account?yahoo_connected=true&yahoo_connection=99")
    assert stored == [{
        "user_id": 7,
        "provider": "yahoo",
        "secrets": {"yahoo_access_token": "at-1", "yahoo_refresh_token": "rt-1",
                    "yahoo_token_expiry": "2026-09-15T13:00:00"},
        "account": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "scope": "fspt-r",
    }]


@pytest.mark.api
def test_callback_asks_yahoo_for_the_account_when_the_token_response_lacks_it(client, stored, monkeypatch):
    monkeypatch.setattr(YahooService, "exchange_code_for_tokens",
                        AsyncMock(return_value={**TOKENS, "guid": ""}))
    monkeypatch.setattr(YahooService, "get_user_guid", AsyncMock(return_value="FROMUSERSCALL"))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert stored[0]["account"] == "FROMUSERSCALL"


@pytest.mark.api
def test_callback_names_a_fantasy_refusal_after_a_good_login(client, stored, monkeypatch):
    """Tokens were issued, so OAuth is fine; the Fantasy API refusing them means
    the app is not enabled for it. Say that, not "oauth failed"."""
    from core.errors import ProviderAuthError

    monkeypatch.setattr(YahooService, "exchange_code_for_tokens",
                        AsyncMock(return_value={**TOKENS, "guid": ""}))
    monkeypatch.setattr(YahooService, "get_user_guid", AsyncMock(side_effect=ProviderAuthError("yahoo")))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert res.headers["location"].endswith("/account?yahoo_error=fantasy_not_authorized")
    assert stored == []


@pytest.mark.api
def test_callback_refuses_a_grant_yahoo_will_not_put_a_name_to(client, stored, monkeypatch):
    """Two nameless accounts would share one row (the pre-0025 shape); nothing is stored."""
    monkeypatch.setattr(YahooService, "exchange_code_for_tokens",
                        AsyncMock(return_value={**TOKENS, "guid": ""}))
    monkeypatch.setattr(YahooService, "get_user_guid", AsyncMock(return_value=""))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert res.headers["location"].endswith("/account?yahoo_error=no_account_id")
    assert stored == []


@pytest.mark.api
def test_the_callback_lands_on_the_path_the_state_carries(client, stored, monkeypatch):
    """A desk starts the connect from where it is and gets the browser back
    there; the path's own query is kept and the callback's parameters follow it."""
    monkeypatch.setattr(YahooService, "validate_state",
                        staticmethod(lambda state: {"user_id": "user_test_123", "return_to": "/week?view=matchup"}))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.status_code in (302, 307)
    assert res.headers["location"].endswith("/week?view=matchup&yahoo_connected=true&yahoo_connection=99")


@pytest.mark.api
def test_a_decline_goes_back_to_the_same_place(client, stored, monkeypatch):
    monkeypatch.setattr(YahooService, "validate_state",
                        staticmethod(lambda state: {"user_id": "user_test_123", "return_to": "/account"}))

    res = client.get("/v1/internal/yahoo/callback?error=access_denied&state=signed", follow_redirects=False)

    assert res.headers["location"].endswith("/account?yahoo_error=access_denied")
    assert stored == []


@pytest.mark.api
def test_a_return_path_in_the_state_is_checked_again_before_use(client, stored, monkeypatch):
    """The signature covers the payload, not what was put in it: a value that
    would leave our origin is dropped even when it arrives signed."""
    monkeypatch.setattr(YahooService, "validate_state",
                        staticmethod(lambda state: {"user_id": "user_test_123", "return_to": "//evil.example/x"}))

    res = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)

    assert res.headers["location"].endswith("/account?yahoo_connected=true&yahoo_connection=99")


@pytest.mark.api
def test_authorize_hands_the_return_path_to_the_state(authed_client, monkeypatch):
    asked: list[tuple] = []

    def fake_get_auth_url(user_id, return_to=None):
        asked.append((user_id, return_to))
        return "https://api.login.yahoo.com/oauth2/request_auth?state=s", "s"

    monkeypatch.setattr(YahooService, "get_auth_url", staticmethod(fake_get_auth_url))

    res = authed_client.get("/v1/internal/yahoo/authorize?return_to=%2Faccount%3Fadd")

    assert res.status_code == 200
    assert res.json()["auth_url"].startswith("https://api.login.yahoo.com/")
    assert asked == [("user_test_123", "/account?add")]


@pytest.mark.api
def test_an_overlong_return_path_falls_back_instead_of_refusing_the_authorize(authed_client, monkeypatch):
    """The path is a nicety; a bad one must not stop the sign-in. It reaches the
    state builder, which drops it, rather than failing validation at the door."""
    asked: list[tuple] = []

    def fake_get_auth_url(user_id, return_to=None):
        asked.append((user_id, return_to))
        return "https://api.login.yahoo.com/oauth2/request_auth?state=s", "s"

    monkeypatch.setattr(YahooService, "get_auth_url", staticmethod(fake_get_auth_url))

    res = authed_client.get("/v1/internal/yahoo/authorize?return_to=/" + "a" * 400)

    assert res.status_code == 200
    assert asked[0][1] == "/" + "a" * 400


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
