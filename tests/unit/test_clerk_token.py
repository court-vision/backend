"""
Unit tests for Clerk JWT verification.

Covers issuer enforcement and the optional `azp` (authorized party) allowlist.
Tokens are signed with a throwaway RSA key that is injected in place of the
JWKS lookup, so no network access is needed.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core import clerk_auth
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
    with pytest.raises(HTTPException) as exc:
        _verify(_make_token(private_pem, iss="https://evil.example.com"))
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_token_without_issuer_is_rejected(rsa_keys, trust_test_key):
    private_pem, _ = rsa_keys
    with pytest.raises(HTTPException) as exc:
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
    with pytest.raises(HTTPException) as exc:
        _verify(_make_token(private_pem, iss=settings.clerk_issuer, azp="https://evil.example.com"))
    assert exc.value.status_code == 401


@pytest.mark.unit
def test_azp_allowlist_accepts_listed_party(rsa_keys, trust_test_key, monkeypatch):
    private_pem, _ = rsa_keys
    monkeypatch.setattr(settings, "clerk_authorized_parties", ["http://localhost:3000"])
    user = _verify(_make_token(private_pem, iss=settings.clerk_issuer, azp="http://localhost:3000"))
    assert user["clerk_user_id"] == "user_abc"
