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
from services.yahoo_service import YahooService
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


# ---- Nor connect Yahoo, a write that starts with a GET ----

@pytest.fixture
def yahoo_flow(monkeypatch):
    """Yahoo's OAuth collaborators, recording any use."""
    calls = []

    def fake_get_auth_url(user_id, return_to=None):
        calls.append(("auth_url", user_id))
        return "https://api.login.yahoo.com/oauth2/request_auth?state=s", "s"

    async def fake_exchange(code):
        calls.append(("exchange", code))
        return {}

    monkeypatch.setattr(YahooService, "get_auth_url", staticmethod(fake_get_auth_url))
    monkeypatch.setattr(YahooService, "exchange_code_for_tokens", staticmethod(fake_exchange))
    monkeypatch.setattr(YahooService, "validate_state", staticmethod(lambda state: {"user_id": APP_MAP_USER}))
    return calls


@pytest.mark.api
def test_app_map_account_cannot_start_a_yahoo_connection(signed_in_as, configured, yahoo_flow):
    client, headers = signed_in_as
    r = client.get("/v1/internal/yahoo/authorize", headers=headers(APP_MAP_USER))
    assert r.status_code == 403
    assert r.json()["error_code"] == "APP_MAP_READ_ONLY"
    assert yahoo_flow == []


@pytest.mark.api
def test_other_users_still_start_a_yahoo_connection(signed_in_as, configured, yahoo_flow):
    client, headers = signed_in_as
    r = client.get("/v1/internal/yahoo/authorize", headers=headers("user_someone"))
    assert r.status_code == 200
    assert yahoo_flow == [("auth_url", "user_someone")]


@pytest.mark.api
def test_a_callback_for_the_app_map_account_stores_nothing(client, configured, yahoo_flow):
    r = client.get("/v1/internal/yahoo/callback?code=abc&state=signed", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].endswith("/account?yahoo_error=read_only")
    assert yahoo_flow == []


# ---- The upload: the workflow stores the map it crawled, behind its own token ----

MAP_URL = "/v1/internal/app-map/maps/{build}"
BUILD = "21a5238c0ffee21a5238c0ffee21a5238c0ffee2"


def _map(**overrides):
    return {
        "version": 1, "build": BUILD, "crawledAt": "2026-10-20T07:00:00Z",
        "desks": [{"id": "week", "label": "Week", "href": "/week", "note": "Your team's matchup week",
                   "keywords": ["market"], "needs": "account", "keys": None}],
        "screens": [{"id": "week", "desk": "week", "title": "Week", "trail": [], "url": "/week", "link": "/week",
                     "linkVaries": [], "tree": "- button \"Market P\"", "fingerprint": "ab12", "signedIn": True}],
        "copy": [{"file": "src/components/week/Market.tsx", "desk": "week", "seen": ["Market"], "unseen": ["No free agents"]}],
        "findings": [],
        **overrides,
    }


@pytest.fixture
def upload_configured(monkeypatch):
    monkeypatch.setattr(settings, "app_map_upload_token", SecretStr("upload-token"))


@pytest.fixture
def stored(monkeypatch):
    calls = []

    async def fake_store(upload, raw, size):
        calls.append((upload, raw, size))
        stats = app_map.app_maps.stats_of(upload, size)
        return None, stats, 2

    monkeypatch.setattr(app_map.app_maps, "store", fake_store)
    return calls


def _put(client, body, build=BUILD, token="upload-token", **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if isinstance(body, (dict, list)):
        return client.put(MAP_URL.format(build=build), headers=headers, json=body, **kwargs)
    return client.put(MAP_URL.format(build=build), headers={**headers, "Content-Type": "application/json"}, content=body, **kwargs)


@pytest.mark.api
def test_upload_is_off_until_its_token_is_set(client, configured, stored):
    # The sign-in route's settings don't turn the upload on
    r = _put(client, _map())
    assert r.status_code == 503
    assert r.json()["error_code"] == "APP_MAP_UPLOAD_DISABLED"
    assert stored == []


@pytest.mark.api
@pytest.mark.parametrize("token,code", [(None, "AUTH_REQUIRED"), ("nope", "INVALID_TOKEN"), ("map-token", "INVALID_TOKEN")])
def test_upload_takes_only_its_own_token(client, configured, upload_configured, stored, token, code):
    # The crawl's sign-in token is not enough to replace the map
    r = _put(client, _map(), token=token)
    assert r.status_code == 401
    assert r.json()["error_code"] == code
    assert stored == []


@pytest.mark.api
def test_the_upload_token_gets_no_sign_in(client, configured, upload_configured, clerk):
    r = client.post(URL, headers={"Authorization": "Bearer upload-token"})
    assert r.status_code == 401
    assert clerk == []


@pytest.mark.api
def test_a_map_is_stored(client, upload_configured, stored):
    r = _put(client, _map())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["build"] == BUILD and data["stored"] is True and data["pruned"] == 2
    assert data["stats"]["screens"] == 1 and data["stats"]["signed_in_screens"] == 1 and data["stats"]["unseen_copy"] == 1
    upload, raw, size = stored[0]
    # Stored as uploaded, extra fields and all
    assert raw["screens"][0]["fingerprint"] == "ab12"
    assert size == data["stats"]["bytes"] > 0


@pytest.mark.api
def test_a_kept_map_says_why(client, upload_configured, monkeypatch):
    async def fake_store(upload, raw, size):
        return "Kept the newer map of this build", app_map.app_maps.stats_of(upload, size), 0

    monkeypatch.setattr(app_map.app_maps, "store", fake_store)
    r = _put(client, _map())
    assert r.status_code == 200
    assert r.json()["message"] == "Kept the newer map of this build"
    assert r.json()["data"]["stored"] is False


@pytest.mark.api
@pytest.mark.parametrize("build", ["main", "21A5238", "21a52", "21a5238c0ffee21a5238c0ffee21a5238c0ffee21a5"])
def test_build_must_be_a_commit(client, upload_configured, stored, build):
    r = _put(client, _map(build=build), build=build)
    assert r.status_code == 422
    assert stored == []


@pytest.mark.api
@pytest.mark.parametrize("body,code", [
    (b"not json", "APP_MAP_INVALID"),
    ([], "APP_MAP_INVALID"),
    (_map(screens=[]), "APP_MAP_INVALID"),
    (_map(desks=[{"id": "week"}]), "APP_MAP_INVALID"),
    (_map(screens=[{"id": "week", "desk": "week"}]), "APP_MAP_INVALID"),
    (_map(version=2), "APP_MAP_VERSION"),
    # A newer version is refused as one, whatever its shape
    (_map(version=2, screens=[{"id": "week", "tree": {"role": "main"}}]), "APP_MAP_VERSION"),
    # A version that only coerces to an int, and a crawl time with no zone
    (_map(version="2"), "APP_MAP_INVALID"),
    (_map(version=2.0), "APP_MAP_INVALID"),
    (_map(crawledAt="2026-10-20T07:00:00"), "APP_MAP_INVALID"),
    (_map(build="0" * 40), "APP_MAP_BUILD_MISMATCH"),
])
def test_anything_but_this_builds_map_is_refused(client, upload_configured, stored, body, code):
    r = _put(client, body)
    assert r.status_code == 400
    assert r.json()["error_code"] == code
    assert stored == []


@pytest.mark.api
def test_a_map_over_the_cap_is_refused(client, upload_configured, stored, monkeypatch):
    monkeypatch.setattr(app_map, "MAX_MAP_BYTES", 1000)
    big = _map(findings=[{"kind": "x", "desk": "week", "detail": "y" * 2000}])
    r = _put(client, big)
    assert r.status_code == 413
    assert r.json()["error_code"] == "APP_MAP_TOO_LARGE"
    assert stored == []


@pytest.mark.api
def test_a_body_with_no_length_is_cut_at_the_cap(monkeypatch):
    """A chunked body declares no length: it is read only up to the cap."""
    import asyncio

    from core.errors import AppError

    monkeypatch.setattr(app_map, "MAX_MAP_BYTES", 10)
    read = []

    class Chunked:
        headers = {}

        async def stream(self):
            for chunk in (b"12345", b"67890", b"overflow", b"never read"):
                read.append(chunk)
                yield chunk

    with pytest.raises(AppError) as e:
        asyncio.run(app_map._body(Chunked()))
    assert e.value.status_code == 413
    assert read == [b"12345", b"67890", b"overflow"]
