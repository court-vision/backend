"""Stateless Yahoo OAuth state is portable across API replicas."""

from freezegun import freeze_time

from core.settings import settings
from services.yahoo.oauth import YahooOAuthService


def test_signed_state_round_trip_and_tamper_rejection(monkeypatch):
    monkeypatch.setattr(settings, "yahoo_client_id", "client-id")
    with freeze_time("2026-08-30T12:00:00Z"):
        auth_url, state = YahooOAuthService.get_auth_url("user_42")
        payload = YahooOAuthService.validate_state(state)

    assert f"state={state}" in auth_url
    assert payload is not None and payload["user_id"] == "user_42"
    assert YahooOAuthService.validate_state(f"{state}x") is None


def test_signed_state_expires_after_ten_minutes(monkeypatch):
    monkeypatch.setattr(settings, "yahoo_client_id", "client-id")
    with freeze_time("2026-08-30T12:00:00Z"):
        _, state = YahooOAuthService.get_auth_url("user_42")
    with freeze_time("2026-08-30T12:11:00Z"):
        assert YahooOAuthService.validate_state(state) is None
