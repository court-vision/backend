"""
Clerk JWT Token Verification Module

This module handles verification of Clerk-issued JWT tokens for API authentication.
It fetches Clerk's JWKS (JSON Web Key Set) and validates tokens using RS256.
"""

import os
import jwt
import requests
from functools import lru_cache
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

# Clerk JWKS URL - set this in your environment variables
# Format: https://<your-clerk-frontend-api>.clerk.accounts.dev/.well-known/jwks.json
CLERK_JWKS_URL = os.environ.get('CLERK_JWKS_URL')

security = HTTPBearer()


@lru_cache(maxsize=1)
def get_clerk_jwks() -> dict:
    """
    Fetch and cache Clerk's JWKS public keys.
    Uses lru_cache to avoid repeated network calls.
    """
    if not CLERK_JWKS_URL:
        raise HTTPException(
            status_code=500,
            detail="CLERK_JWKS_URL environment variable not configured"
        )

    try:
        response = requests.get(CLERK_JWKS_URL, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Failed to fetch Clerk JWKS: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch authentication keys"
        )


def get_public_key_for_token(token: str):
    """
    Get the RSA public key that matches the token's 'kid' (key ID) header.
    """
    jwks = get_clerk_jwks()

    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as e:
        print(f"Failed to decode token header: {e}")
        raise HTTPException(status_code=401, detail="Invalid token format")

    kid = unverified_header.get('kid')
    if not kid:
        raise HTTPException(status_code=401, detail="Token missing key ID")

    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)

    # If key not found, clear cache and retry once (key rotation)
    get_clerk_jwks.cache_clear()
    jwks = get_clerk_jwks()

    for key in jwks.get('keys', []):
        if key.get('kid') == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key)

    raise HTTPException(status_code=401, detail="Unable to find appropriate signing key")


def verify_clerk_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    Verify a Clerk JWT token and return the payload.

    Use this as a FastAPI dependency in protected routes:
        @router.get('/protected')
        async def protected_route(current_user: dict = Depends(verify_clerk_token)):
            ...

    Returns:
        dict with 'clerk_user_id' (sub claim), 'email', and other token claims
    """
    token = credentials.credentials

    try:
        public_key = get_public_key_for_token(token)

        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={
                "verify_aud": False,  # Clerk doesn't use aud claim by default
                "verify_iss": False,  # Skip issuer verification for flexibility
            }
        )

        # Extract and normalize user info from Clerk token
        return {
            "clerk_user_id": payload.get("sub"),  # Clerk user ID
            "email": payload.get("email"),
            "exp": payload.get("exp"),
            "iat": payload.get("iat"),
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as e:
        print(f"Token validation error: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        print(f"Unexpected authentication error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
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
