"""
Clerk JWT Token Verification Module

This module handles verification of Clerk-issued JWT tokens for API authentication.
It fetches Clerk's JWKS (JSON Web Key Set) and validates tokens using RS256.

Failures raise `core.errors` exceptions, rendered as the standard envelope:
missing/invalid/expired tokens are 401 (`AUTH_REQUIRED`, `INVALID_TOKEN`,
`TOKEN_EXPIRED`); a JWKS outage is 503 `AUTH_UNAVAILABLE`; a write by the app
map's read-only account is 403 `APP_MAP_READ_ONLY`.

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
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

from core.errors import AppError, AuthenticationError, AuthorizationError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from core.tracing import timed

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

# Clerk's signing keys by kid, built from the JWKS once and shared by every
# thread. A plain lru_cache let each of the 9 parallel requests in the first
# Week open after a deploy make its own fetch (about 330 ms each in prod); the
# lock makes them wait for one.
_jwks: Optional[dict[str, rsa.RSAPublicKey]] = None
_jwks_lock = threading.Lock()
# Fetches finished so far, and whether the last one failed. Callers that queued
# on the lock behind a fetch share its failure too. Otherwise, with the cache
# cold and Clerk down, they fetched one after another: the k-th caller waited k
# fetch timeouts (up to 20 s each) for its 503 while holding a threadpool thread
# that pipeline auth also needs, where each used to wait for one.
_jwks_fetches = 0
_jwks_last_fetch_failed = False


def _usable_keys(entries: list[dict]) -> dict[str, rsa.RSAPublicKey]:
    """The entries that build into an RSA public key, by kid; the rest are dropped.

    Each key is built here, once per fetch, not on every request. An entry that
    could not be built (a partial `{"kid": ...}` with no `n`/`e`, say) used to
    be cached as it came, and every token naming its kid then failed to build
    it: 401 INVALID_TOKEN on every request until a restart, even after Clerk
    recovered, because only an unknown kid refetches. Dropped, its kid is
    unknown, so a token naming it refetches the keys like after a rotation.
    """
    keys: dict[str, rsa.RSAPublicKey] = {}
    for entry in entries:
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            # No token can name it: a token without a kid is refused before the lookup
            log.warning("clerk_jwks_key_unusable", kid=None, reason="no kid")
            continue
        try:
            key = jwt.algorithms.RSAAlgorithm.from_jwk(entry)
        except Exception as e:
            # PyJWT and cryptography raise InvalidKeyError, ValueError or TypeError
            # depending on what is missing or wrong; any of them means unusable.
            log.warning("clerk_jwks_key_unusable", kid=kid, reason=type(e).__name__)
            continue
        if not isinstance(key, rsa.RSAPublicKey):
            # A private key (an entry with `d`) builds, but cannot verify a signature
            log.warning("clerk_jwks_key_unusable", kid=kid, reason=type(key).__name__)
            continue
        keys.setdefault(kid, key)   # the first entry for a kid wins, as the lookup always did
    return keys


def _fetch_jwks() -> dict[str, rsa.RSAPublicKey]:
    if not CLERK_JWKS_URL:
        log.error("clerk_jwks_url_missing")
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication is not configured")

    try:
        with timed("clerk", "GET clerk jwks"):
            response = _clerk_http.get(CLERK_JWKS_URL, timeout=10)
        response.raise_for_status()
        jwks = response.json()
    except requests.RequestException as e:  # includes a body that is not JSON
        log.error("clerk_jwks_fetch_failed", error=type(e).__name__, detail=str(e))
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")

    # A 200 whose JSON is not a key set (null, a list, an error object), or whose
    # keys cannot verify anything, must not be cached as one: every token would
    # then fail on it until the next deploy. Refused, it is a failed fetch: the
    # keys already cached stay, and the next caller fetches again.
    entries = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        log.error("clerk_jwks_malformed", body_type=type(jwks).__name__)
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")
    keys = _usable_keys(entries)
    if not keys:
        log.error("clerk_jwks_malformed", body_type=type(jwks).__name__, entries=len(entries), usable=0)
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")
    return keys


def get_clerk_jwks(replacing: Optional[dict] = None) -> dict[str, rsa.RSAPublicKey]:
    """
    Clerk's JWKS public keys by kid, fetched once and cached for the process.
    Only keys that built are cached (see `_usable_keys`).

    `replacing` is a key set the caller found no key in for its token (Clerk
    rotated its keys). The keys are fetched again only while the cache still
    holds that set: a caller that finds another thread has already replaced
    it uses the new set.

    Concurrent callers make one fetch between them and share its outcome: the
    keys, or a 503 when it fails. A caller that arrives after a failed fetch
    tries again, so the keys are still fetched lazily once Clerk answers. A
    failed fetch keeps the keys already cached.
    """
    global _jwks, _jwks_fetches, _jwks_last_fetch_failed
    jwks = _jwks
    if jwks is not None and jwks is not replacing:
        return jwks
    fetches_seen = _jwks_fetches
    with _jwks_lock:
        jwks = _jwks
        if jwks is not None and jwks is not replacing:
            return jwks
        if _jwks_fetches != fetches_seen and _jwks_last_fetch_failed:
            # A fetch failed while this caller waited for the lock; that was its fetch too.
            raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")
        failed = True
        try:
            _jwks = _fetch_jwks()
            failed = False
        finally:
            _jwks_fetches += 1
            _jwks_last_fetch_failed = failed
        return _jwks


def warm_clerk_jwks() -> None:
    """Fetch the JWKS before the first request (application startup).

    Best effort: a failure is logged and the first request fetches the keys
    itself, as it always has.
    """
    try:
        keys = len(get_clerk_jwks())
    except Exception as e:
        log.warning("clerk_jwks_warm_failed", error=type(e).__name__)
        return
    log.info("clerk_jwks_warmed", keys=keys)


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
    key = jwks.get(kid)
    if key is not None:
        return key

    # If key not found, refresh the keys and retry once (key rotation). A key
    # that was dropped because it could not be built lands here too.
    key = get_clerk_jwks(replacing=jwks).get(kid)
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
        with timed("clerk", "GET clerk user"):
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


def create_sign_in_token(clerk_user_id: str, expires_in_seconds: int) -> str:
    """
    A one-time sign-in ticket for one user, from Clerk's Backend API.

    The browser redeems it with Clerk (`signIn.create({strategy: "ticket"})`),
    so whoever holds it signs in as that user without a password, once, until
    it expires. Only the app map's sign-in route calls this, for the app map's
    own account. The ticket is never logged.
    """
    if not CLERK_SECRET_KEY:
        log.error("clerk_secret_key_missing")
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication is not configured")

    try:
        with timed("clerk", "POST clerk sign_in_tokens"):
            response = _clerk_http.post(
                "https://api.clerk.com/v1/sign_in_tokens",
                headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
                json={"user_id": clerk_user_id, "expires_in_seconds": expires_in_seconds},
                timeout=10,
            )
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as e:  # includes a body that is not JSON
        status = e.response.status_code if e.response is not None else None
        log.error("clerk_sign_in_token_failed", error=type(e).__name__, status=status)
        raise ServiceUnavailableError("SIGN_IN_TOKEN_UNAVAILABLE", "A sign-in token could not be created")

    ticket = body.get("token") if isinstance(body, dict) else None
    if not isinstance(ticket, str) or not ticket:
        log.error("clerk_sign_in_token_malformed", body_type=type(body).__name__)
        raise ServiceUnavailableError("SIGN_IN_TOKEN_UNAVAILABLE", "A sign-in token could not be created")
    return ticket


def _require_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("AUTH_REQUIRED", "Authentication required")
    return credentials.credentials


# What the app map's account may do: read. CORS preflights never reach auth.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_app_map_user(clerk_user_id: Optional[str]) -> bool:
    """Whether this is the app map's read-only account (settings.app_map_clerk_user_id)."""
    app_map_user = settings.app_map_clerk_user_id
    return bool(app_map_user) and clerk_user_id == app_map_user


def _refuse_app_map_writes(clerk_user_id: Optional[str], request: Optional[Request]) -> None:
    """The app map's account only reads.

    The crawler that signs in as it already aborts every request but a GET, so
    this is the second wall: whatever reaches the API with its session, from
    the crawl or from anyone else signed in as it, changes nothing. A change
    that starts with a GET (connecting Yahoo) checks `is_app_map_user` itself.
    """
    if request is None or not is_app_map_user(clerk_user_id):
        return
    if request.method in _READ_METHODS:
        return
    log.info("app_map_write_refused", method=request.method, path=request.url.path)
    raise AuthorizationError("APP_MAP_READ_ONLY", "This account is read-only")


def verify_clerk_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    request: Request = None,
) -> dict:
    """
    Verify a Clerk JWT token and return the payload.

    Use this as a FastAPI dependency in protected routes:
        @router.get('/protected')
        async def protected_route(current_user: dict = Depends(verify_clerk_token)):
            ...

    FastAPI always passes `request`; it is what the app map's read-only
    account is checked against (403 APP_MAP_READ_ONLY on a write). A direct
    call without it only verifies the token.

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

        user = {
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

    _refuse_app_map_writes(user["clerk_user_id"], request)
    return user


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    request: Request = None,
) -> dict:
    """
    Wrapper around verify_clerk_token for backward compatibility.

    This function provides the same interface as the old JWT-based get_current_user,
    making it easier to migrate existing routes.

    Returns:
        dict with 'clerk_user_id' (use this instead of old 'uid') and 'email'
    """
    return verify_clerk_token(credentials, request)


def clear_jwks_cache():
    """
    Clear the JWKS cache. Call this if you need to force a refresh
    of Clerk's public keys (e.g., after key rotation).
    """
    global _jwks
    with _jwks_lock:
        _jwks = None
