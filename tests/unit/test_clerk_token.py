"""
Unit tests for Clerk JWT verification.

Covers issuer enforcement, the optional `azp` (authorized party) allowlist,
and that verification never calls Clerk's Backend API: the email is the
token's claim or nothing. Tokens are signed with a throwaway RSA key that is
injected in place of the JWKS lookup, so no network access is needed.
"""

import json
import time
from types import SimpleNamespace

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from core import clerk_auth
from core.errors import AuthenticationError, AuthorizationError, ServiceUnavailableError
from core.settings import settings


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


@pytest.fixture
def trust_test_key(monkeypatch, rsa_keys):
    _, public_key = rsa_keys
    monkeypatch.setattr(clerk_auth, "get_public_key_for_token", lambda token: public_key)


def _make_token(private_pem: bytes, **claims) -> str:
    now = int(time.time())
    payload = {
        "sub": "user_abc",
        "email": "abc@example.com",
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-kid"})


def _verify(token: str) -> dict:
    return clerk_auth.verify_clerk_token(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )


@pytest.mark.unit
def test_issuer_is_derived_from_jwks_url():
    assert settings.clerk_issuer
    assert settings.clerk_jwks_url.startswith(settings.clerk_issuer)
    assert not settings.clerk_issuer.endswith("jwks.json")


@pytest.mark.unit
def test_token_with_expected_issuer_is_accepted(rsa_keys, trust_test_key):
    private_pem, _ = rsa_keys
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer))
    assert user["clerk_user_id"] == "user_abc"
    assert user["email"] == "abc@example.com"


@pytest.mark.unit
def test_token_with_wrong_issuer_is_rejected(rsa_keys, trust_test_key):
    private_pem, _ = rsa_keys
    with pytest.raises(AuthenticationError) as exc:
        _verify(_make_token(private_pem, iss="https://evil.example.com"))
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_token_without_issuer_is_rejected(rsa_keys, trust_test_key):
    private_pem, _ = rsa_keys
    with pytest.raises(AuthenticationError) as exc:
        _verify(_make_token(private_pem))
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_azp_check_is_disabled_when_allowlist_empty(rsa_keys, trust_test_key, monkeypatch):
    private_pem, _ = rsa_keys
    monkeypatch.setattr(settings, "clerk_authorized_parties", [])
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer, azp="https://anything.example.com"))
    assert user["clerk_user_id"] == "user_abc"


@pytest.mark.unit
def test_azp_allowlist_rejects_unknown_party(rsa_keys, trust_test_key, monkeypatch):
    private_pem, _ = rsa_keys
    monkeypatch.setattr(settings, "clerk_authorized_parties", ["http://localhost:3000"])
    with pytest.raises(AuthenticationError) as exc:
        _verify(_make_token(private_pem, iss=settings.clerk_issuer, azp="https://evil.example.com"))
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_azp_allowlist_accepts_listed_party(rsa_keys, trust_test_key, monkeypatch):
    private_pem, _ = rsa_keys
    monkeypatch.setattr(settings, "clerk_authorized_parties", ["http://localhost:3000"])
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer, azp="http://localhost:3000"))
    assert user["clerk_user_id"] == "user_abc"


@pytest.mark.unit
def test_missing_credentials_is_401_auth_required():
    with pytest.raises(AuthenticationError) as exc:
        clerk_auth.verify_clerk_token(None)
    assert exc.value.status_code == 401
    assert exc.value.error_code == "AUTH_REQUIRED"


@pytest.mark.unit
def test_expired_token_is_401_token_expired(rsa_keys, trust_test_key):
    private_pem, _ = rsa_keys
    with pytest.raises(AuthenticationError) as exc:
        _verify(_make_token(private_pem, iss=settings.clerk_issuer, exp=int(time.time()) - 60))
    assert exc.value.error_code == "TOKEN_EXPIRED"


@pytest.mark.unit
def test_malformed_token_is_401_invalid_token():
    with pytest.raises(AuthenticationError) as exc:
        _verify("not-a-jwt")
    assert exc.value.status_code == 401
    assert exc.value.error_code == "INVALID_TOKEN"


@pytest.fixture
def no_clerk_api(monkeypatch):
    """Fail the test if anything reaches Clerk over HTTP."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected Clerk HTTP call: {args[:1]}")
    monkeypatch.setattr(clerk_auth._clerk_http, "get", refuse)
    monkeypatch.setattr(clerk_auth, "fetch_clerk_user", refuse)


@pytest.mark.unit
def test_token_without_email_claim_is_verified_without_calling_clerk(rsa_keys, trust_test_key, no_clerk_api):
    """Verification used to fetch the email from Clerk's Backend API on every
    request whose token had no claim: about 100 ms alone and 300-480 ms in a
    desk-open burst. The email is only needed to create or link a user, so it
    is fetched there (UserSyncService.get_or_create_user)."""
    private_pem, _ = rsa_keys
    now = int(time.time())
    token = jwt.encode(
        {"sub": "user_abc", "iss": settings.clerk_issuer, "iat": now, "exp": now + 300},
        private_pem, algorithm="RS256", headers={"kid": "test-kid"},
    )
    user = _verify(token)
    assert user["clerk_user_id"] == "user_abc"
    assert user["email"] is None


@pytest.mark.unit
def test_empty_email_claim_reads_as_no_email(rsa_keys, trust_test_key, no_clerk_api):
    """The session-token template renders a user without a primary email as ""."""
    private_pem, _ = rsa_keys
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer, email=""))
    assert user["email"] is None


@pytest.mark.unit
def test_email_claim_is_passed_through_without_calling_clerk(rsa_keys, trust_test_key, no_clerk_api):
    private_pem, _ = rsa_keys
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer, email="new@example.com"))
    assert user["email"] == "new@example.com"


@pytest.mark.unit
def test_clerk_calls_share_one_cookieless_session():
    """One Session keeps the connection to Clerk alive between the calls that
    remain (the audit saw a new TLS connection to api.clerk.com per request).
    Its threads share the cookie jar, so the jar accepts nothing."""
    assert isinstance(clerk_auth._clerk_http, requests.Session)
    policy = clerk_auth._clerk_http.cookies.get_policy()
    assert policy.is_not_allowed("api.clerk.com")
    assert policy.is_not_allowed(".clerk.courtvision.dev")


@pytest.mark.unit
def test_user_fetch_goes_through_the_shared_session(monkeypatch):
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({
            "id": "user_abc",
            "primary_email_address_id": "e2",
            "email_addresses": [{"id": "e1", "email_address": "old@example.com"},
                                {"id": "e2", "email_address": "primary@example.com"}],
        }).encode()
        return response

    monkeypatch.setattr(clerk_auth._clerk_http, "get", get)
    monkeypatch.setattr(clerk_auth, "CLERK_SECRET_KEY", "sk_test_fake")
    user = clerk_auth.fetch_clerk_user("user_abc")
    assert calls == ["https://api.clerk.com/v1/users/user_abc"]
    assert user["email"] == "primary@example.com"


# ---- The app map's account is read-only ----

APP_MAP_USER = "user_app_map"


def _request(method: str, path: str = "/v1/internal/teams/add") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""})


@pytest.fixture
def app_map_account(monkeypatch):
    monkeypatch.setattr(settings, "app_map_clerk_user_id", APP_MAP_USER)


def _app_map_credentials(private_pem: bytes, sub: str = APP_MAP_USER) -> HTTPAuthorizationCredentials:
    token = _make_token(private_pem, iss=settings.clerk_issuer, sub=sub)
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.unit
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("dependency", ["verify_clerk_token", "get_current_user"])
def test_app_map_account_cannot_write(rsa_keys, trust_test_key, app_map_account, method, dependency):
    private_pem, _ = rsa_keys
    with pytest.raises(AuthorizationError) as exc:
        getattr(clerk_auth, dependency)(_app_map_credentials(private_pem), _request(method))
    assert exc.value.status_code == 403
    assert exc.value.error_code == "APP_MAP_READ_ONLY"


@pytest.mark.unit
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_app_map_account_can_read(rsa_keys, trust_test_key, app_map_account, method):
    private_pem, _ = rsa_keys
    user = clerk_auth.get_current_user(_app_map_credentials(private_pem), _request(method))
    assert user["clerk_user_id"] == APP_MAP_USER


@pytest.mark.unit
def test_other_users_still_write(rsa_keys, trust_test_key, app_map_account):
    private_pem, _ = rsa_keys
    user = clerk_auth.verify_clerk_token(_app_map_credentials(private_pem, sub="user_abc"), _request("POST"))
    assert user["clerk_user_id"] == "user_abc"


@pytest.mark.unit
def test_nobody_is_read_only_without_an_app_map_account(rsa_keys, trust_test_key, monkeypatch):
    private_pem, _ = rsa_keys
    monkeypatch.setattr(settings, "app_map_clerk_user_id", None)
    user = clerk_auth.verify_clerk_token(_app_map_credentials(private_pem), _request("POST"))
    assert user["clerk_user_id"] == APP_MAP_USER


# ---- One-time sign-in tokens ----

def _clerk_response(status: int, body) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.clerk.com/v1/sign_in_tokens"
    response._content = json.dumps(body).encode()
    return response


@pytest.fixture
def clerk_post(monkeypatch):
    """Answers Clerk's sign_in_tokens POST with `clerk_post.response` and records each call."""
    fake = SimpleNamespace(
        response=_clerk_response(200, {"object": "sign_in_token", "token": "ticket-abc", "status": "pending"}),
        calls=[],
    )

    def post(url, **kwargs):
        fake.calls.append({"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")})
        return fake.response

    monkeypatch.setattr(clerk_auth._clerk_http, "post", post)
    monkeypatch.setattr(clerk_auth, "CLERK_SECRET_KEY", "sk_test_fake")
    return fake


@pytest.mark.unit
def test_sign_in_token_is_created_for_the_named_user(clerk_post):
    ticket = clerk_auth.create_sign_in_token(APP_MAP_USER, 300)
    assert ticket == "ticket-abc"
    assert clerk_post.calls == [{
        "url": "https://api.clerk.com/v1/sign_in_tokens",
        "headers": {"Authorization": "Bearer sk_test_fake"},
        "json": {"user_id": APP_MAP_USER, "expires_in_seconds": 300},
    }]


@pytest.mark.unit
@pytest.mark.parametrize("response", [
    _clerk_response(404, {"errors": [{"code": "resource_not_found"}]}),
    _clerk_response(200, {"object": "sign_in_token"}),
    _clerk_response(200, ["token"]),
])
def test_sign_in_token_failure_is_503(clerk_post, response):
    clerk_post.response = response
    with pytest.raises(ServiceUnavailableError) as exc:
        clerk_auth.create_sign_in_token(APP_MAP_USER, 300)
    assert exc.value.error_code == "SIGN_IN_TOKEN_UNAVAILABLE"


@pytest.mark.unit
def test_sign_in_token_without_a_secret_key_is_503(clerk_post, monkeypatch):
    monkeypatch.setattr(clerk_auth, "CLERK_SECRET_KEY", "")
    with pytest.raises(ServiceUnavailableError) as exc:
        clerk_auth.create_sign_in_token(APP_MAP_USER, 300)
    assert exc.value.error_code == "AUTH_UNAVAILABLE"
    assert clerk_post.calls == []
