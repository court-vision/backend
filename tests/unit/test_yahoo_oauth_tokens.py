"""What the Yahoo token exchange keeps: the account guid that keys the
connection row and the scope the grant was for."""

import pytest
from pydantic import SecretStr

from core.settings import settings
from services.yahoo import oauth
from services.yahoo.oauth import YAHOO_SCOPE, YahooOAuthService

TOKEN_RESPONSE = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "expires_in": 3600,
    "token_type": "bearer",
    "xoauth_yahoo_guid": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "yahoo_client_id", "client-id")
    monkeypatch.setattr(settings, "yahoo_client_secret", SecretStr("client-secret"))


def _posting(monkeypatch, response: dict) -> list[dict]:
    sent: list[dict] = []

    async def fake_post(provider, url, *, headers, data, expect_key, auth_statuses):
        sent.append({"provider": provider, "url": url, "data": data})
        return dict(response)

    monkeypatch.setattr(oauth, "provider_post", fake_post)
    return sent


@pytest.mark.unit
def test_the_exchange_keeps_the_guid_and_records_the_requested_scope(configured, monkeypatch):
    sent = _posting(monkeypatch, TOKEN_RESPONSE)

    tokens = _run(YahooOAuthService.exchange_code_for_tokens("code-1"))

    assert sent[0]["provider"] == "yahoo" and sent[0]["data"]["grant_type"] == "authorization_code"
    assert tokens["guid"] == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    assert tokens["scope"] == YAHOO_SCOPE
    assert tokens["access_token"] == "at-1" and tokens["refresh_token"] == "rt-1"


@pytest.mark.unit
def test_a_scope_yahoo_echoes_wins_over_the_requested_one(configured, monkeypatch):
    _posting(monkeypatch, {**TOKEN_RESPONSE, "scope": "fspt-w"})

    tokens = _run(YahooOAuthService.exchange_code_for_tokens("code-1"))

    assert tokens["scope"] == "fspt-w"


@pytest.mark.unit
def test_a_response_without_a_guid_yields_an_empty_account(configured, monkeypatch):
    """Empty, never a crash: the callback then asks Yahoo who the account is."""
    _posting(monkeypatch, {k: v for k, v in TOKEN_RESPONSE.items() if k != "xoauth_yahoo_guid"})

    assert _run(YahooOAuthService.exchange_code_for_tokens("code-1"))["guid"] == ""


@pytest.mark.unit
def test_the_login_guid_is_read_from_yahoos_users_payload():
    from services.yahoo.discovery import login_guid

    payload = {"fantasy_content": {"users": {"0": {"user": [{"guid": "ABC123"}, {"games": {}}]}, "count": 1}}}
    assert login_guid(payload) == "ABC123"
    assert login_guid({"fantasy_content": {"users": {"count": 0}}}) == ""
    assert login_guid({}) == ""


@pytest.mark.unit
def test_the_authorize_url_asks_for_the_approved_scope(configured):
    auth_url, _ = YahooOAuthService.get_auth_url("user_42")
    assert f"scope={YAHOO_SCOPE}" in auth_url
    assert YAHOO_SCOPE == "fspt-r"          # Read only until Yahoo approves Read/Write


def _run(coro):
    import asyncio

    return asyncio.run(coro)
