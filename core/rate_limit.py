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
from starlette.responses import JSONResponse

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


# Create limiter with custom key function
limiter = Limiter(key_func=get_rate_limit_key)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Custom handler for rate limit exceeded errors."""
    return JSONResponse(
        status_code=429,
        content={
            "status": ApiStatus.RATE_LIMITED.value,
            "message": f"Rate limit exceeded: {exc.detail}",
            "data": None,
        },
    )


# Rate limit constants for easy reference
PUBLIC_RATE_LIMIT = "100/minute"
API_KEY_RATE_LIMIT = "1000/minute"
