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

# Clerk Secret Key - required for fetching user details from Clerk's API
CLERK_SECRET_KEY = os.environ.get('CLERK_SECRET_KEY')

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
        print("Warning: CLERK_SECRET_KEY not configured, cannot fetch user details")
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
        print(f"Failed to fetch user from Clerk API: {e}")
        return None


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
