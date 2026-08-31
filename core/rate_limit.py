"""
Rate limiting for Court Vision public API.

Uses slowapi to enforce request limits:
- Public endpoints: 100 requests/minute
- API Key endpoints: 1000 requests/minute
"""

from fastapi import Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from core.errors import RATE_LIMITED_CODE
from core.middleware import error_json_response
from core.settings import settings
from schemas.common import ApiStatus


def get_rate_limit_key(request: Request) -> str:
    """
    Get rate limit key - uses API key if present, otherwise IP address.
    This allows API key users to have separate (higher) rate limits.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        # Use first 11 chars (prefix) to avoid storing full key in memory
        return f"api_key:{api_key[:11]}"
    return get_remote_address(request)


# Redis is mandatory in deployed Railway environments (validated by Settings).
# The memory backend remains convenient for local development and isolated tests.
_storage_uri = (
    settings.rate_limit_storage_uri.get_secret_value()
    if settings.rate_limit_storage_uri
    else "memory://"
)
limiter = Limiter(
    key_func=get_rate_limit_key,
    storage_uri=_storage_uri,
    in_memory_fallback_enabled=False,
    key_prefix="courtvision",
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """429 in the standard envelope (`error_code: RATE_LIMITED`, correlation headers)."""
    return error_json_response(
        request,
        status_code=429,
        api_status=ApiStatus.RATE_LIMITED,
        error_code=RATE_LIMITED_CODE,
        message=f"Rate limit exceeded: {exc.detail}",
    )


# Rate limit constants for easy reference
PUBLIC_RATE_LIMIT = "100/minute"
SQLMATE_QUERY_RATE_LIMIT = "30/minute"
API_KEY_RATE_LIMIT = "1000/minute"
