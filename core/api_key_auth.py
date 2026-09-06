"""
API key authentication for protected endpoints.
"""

from typing import Callable
from dataclasses import dataclass

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from core.errors import AuthenticationError, AuthorizationError
from db.models.api_keys import APIKey
from db.base import run_db

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class APIKeyContext:
    user_id: int
    scopes: tuple[str, ...]
    rate_limit_identity: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _verify_key(raw_key: str) -> APIKeyContext | None:
    record = APIKey.verify_key(raw_key)
    if record is None:
        return None
    return APIKeyContext(
        user_id=record.user_id,
        scopes=tuple(record.scopes or ()),
        rate_limit_identity=str(record.id),
    )


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> APIKeyContext:
    """
    Verify API key from request header.

    Raises:
        AuthenticationError (401): the key is missing, invalid, or expired.
    """
    if not api_key:
        raise AuthenticationError("AUTH_REQUIRED", "API key required. Include X-API-Key header.")

    key_record = await run_db("auth.verify_api_key", _verify_key, api_key)
    if not key_record:
        raise AuthenticationError("INVALID_API_KEY", "Invalid or expired API key")

    # The persisted API key ID is an opaque, stable identifier. Using it avoids
    # retaining or deriving another value from the raw credential.
    request.state.api_key_identity = key_record.rate_limit_identity
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
        api_key: APIKeyContext = Security(verify_api_key),
    ) -> APIKeyContext:
        if not api_key.has_scope(scope):
            raise AuthorizationError("API_KEY_SCOPE", f"API key lacks required scope: {scope}")
        return api_key

    return checker
