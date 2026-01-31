"""
API key authentication for protected endpoints.
"""

from typing import Callable

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from db.models.api_keys import APIKey

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str | None = Security(api_key_header),
) -> APIKey:
    """
    Verify API key from request header.

    Raises:
        HTTPException: If API key is missing, invalid, or expired.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Include X-API-Key header.",
        )

    key_record = APIKey.verify_key(api_key)
    if not key_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )

    return key_record


def require_scope(scope: str) -> Callable:
    """
    Create a dependency that requires a specific scope.

    Usage:
        @router.post("/optimize")
        async def optimize(api_key: APIKey = Security(require_scope("optimize"))):
            ...
    """

    async def checker(
        api_key: APIKey = Security(verify_api_key),
    ) -> APIKey:
        if not api_key.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key lacks required scope: {scope}",
            )
        return api_key

    return checker
