"""
/v1/internal/app-map/sign-in-token: the app map's one-time sign-in, behind
APP_MAP_TOKEN. Off until the account and token are both configured; the right
bearer gets a ticket for the configured account and no other. Also the other
half: a session as that account reaches no write route.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.v1.internal import app_map
from core import clerk_auth
from core.settings import settings
from tests.api.conftest import make_test_app

URL = "/v1/internal/app-map/sign-in-token"
APP_MAP_USER = "user_app_map"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "app_map_clerk_user_id", APP_MAP_USER)
    monkeypatch.setattr(settings, "app_map_token", SecretStr("map-token"))


@pytest.fixture
def clerk(monkeypatch):
    calls = []

    def fake(clerk_user_id, expires_in_seconds):
        calls.append((clerk_user_id, expires_in_seconds))
        return "ticket-abc"

    monkeypatch.setattr(app_map, "create_sign_in_token", fake)
    return calls


@pytest.mark.api
@pytest.mark.parametrize("unset", ["app_map_clerk_user_id", "app_map_token"])
def test_off_until_configured(client, configured, clerk, monkeypatch, unset):
    monkeypatch.setattr(settings, unset, None)
    r = client.post(URL, headers={"Authorization": "Bearer map-token"})
    assert r.status_code == 503
    assert r.json()["error_code"] == "APP_MAP_DISABLED"
    assert clerk == []


@pytest.mark.api
def test_missing_bearer_is_401(client, configured, clerk):
    r = client.post(URL)
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"
    assert clerk == []


@pytest.mark.api
@pytest.mark.parametrize("bearer", ["nope", "map-token-and-more", "pipe-token"])
def test_wrong_bearer_is_401(client, configured, clerk, bearer):
    r = client.post(URL, headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_TOKEN"
    assert clerk == []


@pytest.mark.api
def test_a_clerk_session_is_not_enough(client, configured, clerk):
    """The test app treats every request as signed in; that reaches nothing here."""
    r = client.post(URL, headers={"Authorization": "Bearer some-clerk-session"})
    assert r.status_code == 401
    assert clerk == []


@pytest.mark.api
def test_right_bearer_gets_a_ticket_for_the_app_map_account(client, configured, clerk):
    # A body naming someone else changes nothing: the route takes no input
    r = client.post(URL, headers={"Authorization": "Bearer map-token"}, json={"user_id": "user_someone_else"})
    assert r.status_code == 200
    assert r.json()["data"] == {"ticket": "ticket-abc", "expires_in_seconds": 300}
    assert r.headers["Cache-Control"] == "no-store"
    assert clerk == [(APP_MAP_USER, 300)]


# ---- A session as the app map's account cannot write ----

@pytest.fixture
def signed_in_as(monkeypatch):
    """A client with real Clerk verification, trusting a throwaway signing key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    monkeypatch.setattr(clerk_auth, "get_public_key_for_token", lambda token: private_key.public_key())
    app = make_test_app()
    app.dependency_overrides.clear()
    client = TestClient(app, raise_server_exceptions=False)

    def headers(sub: str) -> dict:
        now = int(time.time())
        token = jwt.encode(
            {"sub": sub, "iss": settings.clerk_issuer, "iat": now, "exp": now + 300},
            pem, algorithm="RS256", headers={"kid": "test-kid"},
        )
        return {"Authorization": f"Bearer {token}"}

    return client, headers


@pytest.mark.api
@pytest.mark.parametrize("method,path", [
    ("post", "/v1/internal/teams/add"),          # get_db_user -> get_current_user
    ("post", "/v1/internal/api-keys/"),          # verify_clerk_token directly
    ("post", "/v1/internal/streamers/find"),     # get_current_user directly
    ("delete", "/v1/internal/api-keys/k1"),
])
def test_app_map_account_writes_are_403(signed_in_as, configured, method, path):
    client, headers = signed_in_as
    r = getattr(client, method)(path, headers=headers(APP_MAP_USER))
    assert r.status_code == 403
    body = r.json()
    assert body["status"] == "authorization_error"
    assert body["error_code"] == "APP_MAP_READ_ONLY"
