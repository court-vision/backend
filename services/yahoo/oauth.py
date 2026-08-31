"""Yahoo OAuth and token lifecycle.

OAuth state is a short-lived, HMAC-authenticated value.  It carries no secrets
and needs no process-local or shared state, so a callback may land on any API
replica.  Yahoo authorization codes are single-use, which supplies replay
protection for the callback after the signature and expiry checks pass.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from core.errors import ServiceUnavailableError
from core.settings import settings
from services.providers.http import provider_post

YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _state_key() -> bytes:
    # The Clerk key is required in every deployment and is already managed as a
    # secret. Domain separation prevents this use from colliding with auth use.
    secret = settings.clerk_secret_key.get_secret_value()
    return hashlib.sha256(f"courtvision:yahoo-state:{secret}".encode()).digest()


class YahooOAuthService:
    @staticmethod
    def get_auth_url(user_id: str) -> tuple[str, str]:
        if not settings.yahoo_client_id:
            raise ServiceUnavailableError("YAHOO_NOT_CONFIGURED", "Yahoo OAuth is not configured")

        now = datetime.now(timezone.utc)
        payload = {
            "user_id": user_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "nonce": secrets.token_urlsafe(16),
        }
        encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = _b64encode(hmac.new(_state_key(), encoded.encode(), hashlib.sha256).digest())
        state = f"{encoded}.{signature}"

        params = {
            "client_id": settings.yahoo_client_id,
            "redirect_uri": settings.yahoo_redirect_uri,
            "response_type": "code",
            "scope": "fspt-r",
            "state": state,
        }
        return f"{YAHOO_AUTH_URL}?{urlencode(params)}", state

    @staticmethod
    def validate_state(state: str) -> Optional[dict]:
        try:
            encoded, supplied = state.split(".", 1)
            expected = _b64encode(hmac.new(_state_key(), encoded.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                return None
            payload = json.loads(_b64decode(encoded))
            expires_at = datetime.fromisoformat(payload["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                return None
            return payload
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    async def exchange_code_for_tokens(code: str) -> dict:
        return await YahooOAuthService._request_tokens({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.yahoo_redirect_uri,
        })

    @staticmethod
    async def refresh_access_token(refresh_token: str) -> dict:
        result = await YahooOAuthService._request_tokens({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        result["refresh_token"] = result.get("refresh_token") or refresh_token
        return result

    @staticmethod
    async def _request_tokens(data: dict[str, str]) -> dict:
        if not settings.yahoo_client_id or not settings.yahoo_client_secret:
            raise ServiceUnavailableError("YAHOO_NOT_CONFIGURED", "Yahoo OAuth is not configured")

        credentials = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret.get_secret_value()}"
        auth_header = base64.b64encode(credentials.encode()).decode()
        token_data = await provider_post(
            "yahoo",
            YAHOO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
            expect_key="access_token",
            auth_statuses=(400, 401, 403),
        )
        expires_in = token_data.get("expires_in", 3600)
        return {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": expires_in,
            "token_expiry": (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
        }

    @staticmethod
    def _get_headers(access_token: str) -> dict:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
