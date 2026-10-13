"""
Clerk JWT Token Verification Module

This module handles verification of Clerk-issued JWT tokens for API authentication.
It fetches Clerk's JWKS (JSON Web Key Set) and validates tokens using RS256.

Failures raise `core.errors` exceptions, rendered as the standard envelope:
missing/invalid/expired tokens are 401 (`AUTH_REQUIRED`, `INVALID_TOKEN`,
`TOKEN_EXPIRED`); a JWKS outage is 503 `AUTH_UNAVAILABLE`.

Verifying a token makes no network call once the JWKS is cached. The user's
email comes from the session token's `email` claim (Clerk dashboard → Sessions
→ Customize session token, live since 2026-10-13). Without the claim the email
is None: it is fetched from Clerk's Backend API only where it is needed, when
`UserSyncService.get_or_create_user` has to create or link a user. Fetching it
here on every request cost about 100 ms alone and 300-480 ms in a desk-open
burst (docs/PERF_AUDIT_2026-10.md, A1).
"""

import threading
from http.cookiejar import DefaultCookiePolicy

import jwt
import requests
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

from core.errors import AppError, AuthenticationError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings

log = get_logger("auth")

# Format: https://<your-clerk-frontend-api>.clerk.accounts.dev/.well-known/jwks.json
CLERK_JWKS_URL = settings.clerk_jwks_url

# Required for fetching user details from Clerk's API
CLERK_SECRET_KEY = settings.clerk_secret_key.get_secret_value()

# auto_error=False: a missing header is our 401 AUTH_REQUIRED, not FastAPI's 403
security = HTTPBearer(auto_error=False)

# One HTTP session for Clerk, so the rare calls that remain reuse a kept-alive
# connection instead of paying DNS + TCP + TLS each time (the audit measured a
# new connection to api.clerk.com per request). Callers are threadpool threads:
# urllib3's pool is thread-safe, and the cookie policy keeps the shared jar
# empty, since nothing here needs cookies (Cloudflare in front of Clerk sets them).
_clerk_http = requests.Session()
_clerk_http.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))

# The JWKS, fetched once and shared by every thread. A plain lru_cache let each
# of the 9 parallel requests in the first Week open after a deploy make its own
# fetch (about 330 ms each in prod); the lock makes them wait for one.
_jwks: Optional[dict] = None
_jwks_lock = threading.Lock()


def _fetch_jwks() -> dict:
    if not CLERK_JWKS_URL:
        log.error("clerk_jwks_url_missing")
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication is not configured")

    try:
        response = _clerk_http.get(CLERK_JWKS_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        log.error("clerk_jwks_fetch_failed", error=type(e).__name__, detail=str(e))
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")


def get_clerk_jwks(replacing: Optional[dict] = None) -> dict:
    """
    Clerk's JWKS public keys, fetched once and cached for the process.

    `replacing` is a key set the caller found no key in for its token (Clerk
    rotated its keys). The keys are fetched again only while the cache still
    holds that set: a caller that finds another thread has already replaced
    it uses the new set. Either way, concurrent callers make one fetch between
    them, and a failed fetch keeps the keys already cached.
    """
    global _jwks
    jwks = _jwks
    if jwks is not None and jwks is not replacing:
        return jwks
    with _jwks_lock:
        jwks = _jwks
        if jwks is not None and jwks is not replacing:
            return jwks
        _jwks = _fetch_jwks()
        return _jwks


def warm_clerk_jwks() -> None:
    """Fetch the JWKS before the first request (application startup).

    Best effort: a failure is logged and the first request fetches the keys
    itself, as it always has.
    """
    try:
        jwks = get_clerk_jwks()
    except Exception as e:
        log.warning("clerk_jwks_warm_failed", error=type(e).__name__)
        return
    log.info("clerk_jwks_warmed", keys=len(jwks.get("keys", [])))


def _key_for(jwks: dict, kid: str):
    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)
    return None


def get_public_key_for_token(token: str):
    """
    Get the RSA public key that matches the token's 'kid' (key ID) header.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as e:
        log.info("clerk_token_header_invalid", reason=type(e).__name__)
        raise AuthenticationError("INVALID_TOKEN", "Invalid token format")

    kid = unverified_header.get('kid')
    if not kid:
        raise AuthenticationError("INVALID_TOKEN", "Token missing key ID")

    # Only well-formed tokens get as far as the (cached) JWKS fetch
    jwks = get_clerk_jwks()
    key = _key_for(jwks, kid)
    if key is not None:
        return key

    # If key not found, refresh the keys and retry once (key rotation)
    key = _key_for(get_clerk_jwks(replacing=jwks), kid)
    if key is not None:
        return key

    raise AuthenticationError("INVALID_TOKEN", "Unable to find appropriate signing key")


def fetch_clerk_user(clerk_user_id: str) -> Optional[dict]:
    """
    Fetch user details from Clerk's Backend API.

    This is used to get user information (like email) that the session token
    does not carry. Request verification never calls it: only
    `UserSyncService.get_or_create_user` does, when it has to create or link
    a user and the token had no email.

    Args:
        clerk_user_id: The Clerk user ID (e.g., 'user_xxx')

    Returns:
        dict with user details including email, or None if fetch fails
    """
    if not CLERK_SECRET_KEY:
        log.warning("clerk_secret_key_missing")
        return None

    try:
        response = _clerk_http.get(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers={
                "Authorization": f"Bearer {CLERK_SECRET_KEY}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        response.raise_for_status()
        user_data = response.json()

        # Extract primary email from Clerk's response
        email = None
        email_addresses = user_data.get('email_addresses', [])

        # Find the primary email
        primary_email_id = user_data.get('primary_email_address_id')
        for email_obj in email_addresses:
            if email_obj.get('id') == primary_email_id:
                email = email_obj.get('email_address')
                break

        # Fallback to first email if no primary
        if not email and email_addresses:
            email = email_addresses[0].get('email_address')

        return {
            "clerk_user_id": user_data.get('id'),
            "email": email,
            "first_name": user_data.get('first_name'),
            "last_name": user_data.get('last_name'),
        }

    except requests.RequestException as e:
        log.warning("clerk_user_fetch_failed", error=type(e).__name__, detail=str(e))
        return None


def _require_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("AUTH_REQUIRED", "Authentication required")
    return credentials.credentials


def verify_clerk_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    Verify a Clerk JWT token and return the payload.

    Use this as a FastAPI dependency in protected routes:
        @router.get('/protected')
        async def protected_route(current_user: dict = Depends(verify_clerk_token)):
            ...

    Returns:
        dict with 'clerk_user_id' (sub claim), 'email' (the token's claim, or
        None), and other token claims
    """
    token = _require_token(credentials)

    try:
        public_key = get_public_key_for_token(token)

        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={
                # Clerk session tokens carry no `aud`; `azp` is the equivalent check below
                "verify_aud": False,
                "verify_iss": True,
            }
        )

        azp = payload.get("azp")
        if settings.clerk_authorized_parties and azp and azp not in settings.clerk_authorized_parties:
            raise AuthenticationError("INVALID_TOKEN", "Invalid authorized party")

        return {
            "clerk_user_id": payload.get("sub"),
            # None when the session token has no email claim. Nothing is fetched
            # here: get_or_create_user fetches it when it creates or links a user.
            "email": payload.get("email") or None,
            "exp": payload.get("exp"),
            "iat": payload.get("iat"),
        }

    except jwt.ExpiredSignatureError:
        raise AuthenticationError("TOKEN_EXPIRED", "Token has expired")
    except jwt.InvalidTokenError as e:
        log.info("clerk_token_invalid", reason=type(e).__name__)
        raise AuthenticationError("INVALID_TOKEN", "Invalid token")
    except AppError:
        raise
    except Exception as e:
        log.exception("clerk_auth_unexpected_error", error=type(e).__name__)
        raise AuthenticationError("INVALID_TOKEN", "Authentication failed")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    Wrapper around verify_clerk_token for backward compatibility.

    This function provides the same interface as the old JWT-based get_current_user,
    making it easier to migrate existing routes.

    Returns:
        dict with 'clerk_user_id' (use this instead of old 'uid') and 'email'
    """
    return verify_clerk_token(credentials)


def clear_jwks_cache():
    """
    Clear the JWKS cache. Call this if you need to force a refresh
    of Clerk's public keys (e.g., after key rotation).
    """
    global _jwks
    with _jwks_lock:
        _jwks = None
