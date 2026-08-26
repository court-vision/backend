"""
Clerk JWT Token Verification Module

This module handles verification of Clerk-issued JWT tokens for API authentication.
It fetches Clerk's JWKS (JSON Web Key Set) and validates tokens using RS256.

Failures raise `core.errors` exceptions, rendered as the standard envelope:
missing/invalid/expired tokens are 401 (`AUTH_REQUIRED`, `INVALID_TOKEN`,
`TOKEN_EXPIRED`); a JWKS outage is 503 `AUTH_UNAVAILABLE`.
"""

import jwt
import requests
from functools import lru_cache
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


@lru_cache(maxsize=1)
def get_clerk_jwks() -> dict:
    """
    Fetch and cache Clerk's JWKS public keys.
    Uses lru_cache to avoid repeated network calls.
    """
    if not CLERK_JWKS_URL:
        log.error("clerk_jwks_url_missing")
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication is not configured")

    try:
        response = requests.get(CLERK_JWKS_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        log.error("clerk_jwks_fetch_failed", error=type(e).__name__, detail=str(e))
        raise ServiceUnavailableError("AUTH_UNAVAILABLE", "Authentication keys could not be fetched")


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

    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)

    # If key not found, clear cache and retry once (key rotation)
    get_clerk_jwks.cache_clear()
    jwks = get_clerk_jwks()

    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)

    raise AuthenticationError("INVALID_TOKEN", "Unable to find appropriate signing key")


def fetch_clerk_user(clerk_user_id: str) -> Optional[dict]:
    """
    Fetch user details from Clerk's Backend API.

    This is used to get user information (like email) that isn't included
    in the JWT by default.

    Args:
        clerk_user_id: The Clerk user ID (e.g., 'user_xxx')

    Returns:
        dict with user details including email, or None if fetch fails
    """
    if not CLERK_SECRET_KEY:
        log.warning("clerk_secret_key_missing")
        return None

    try:
        response = requests.get(
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
        dict with 'clerk_user_id' (sub claim), 'email', and other token claims
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

        clerk_user_id = payload.get("sub")
        email = payload.get("email")  # May be None if not in JWT claims

        # If email not in JWT, fetch from Clerk's API
        if not email and clerk_user_id:
            clerk_user = fetch_clerk_user(clerk_user_id)
            if clerk_user:
                email = clerk_user.get("email")

        return {
            "clerk_user_id": clerk_user_id,
            "email": email,
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
    get_clerk_jwks.cache_clear()
