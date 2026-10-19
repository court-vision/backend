"""
The app map's bearer token (docs/APP_MAP_PLAN.md in court-vision/docs).

The frontend's App Map workflow presents APP_MAP_TOKEN to get a one-time
sign-in as the app map's account and nothing else: it is a token of its own,
not the pipeline token, so the crawl can reach no other service route and
cron-runner cannot sign in as anyone. Until both APP_MAP_TOKEN and
APP_MAP_CLERK_USER_ID are set, its routes answer 503 APP_MAP_DISABLED.
"""

import hmac
from typing import Optional

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.errors import AuthenticationError, ServiceUnavailableError
from core.settings import settings

# auto_error=False: a missing header is our 401 AUTH_REQUIRED, not FastAPI's 403
security = HTTPBearer(auto_error=False)


def verify_app_map_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> None:
    expected = settings.app_map_token.get_secret_value() if settings.app_map_token else ""
    if not expected or not settings.app_map_clerk_user_id:
        raise ServiceUnavailableError("APP_MAP_DISABLED", "The app map account is not configured", log_level="warning")
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("AUTH_REQUIRED", "Authentication required")
    if not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise AuthenticationError("INVALID_TOKEN", "Invalid app map token")
