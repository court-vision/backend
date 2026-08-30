"""
Pydantic schemas for API key management endpoints.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from schemas.common import ApiModel, BaseResponse


class ApiKeyListItem(ApiModel):
    """Single API key item for list responses (never includes the raw key or hash)."""
    id: str
    name: str
    key_prefix: str
    scopes: list[str]
    rate_limit: int
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    is_active: bool


class CreateApiKeyRequest(ApiModel):
    """Request body for creating a new API key."""
    name: str
    scopes: list[str] = ["read"]
    expires_days: int | None = None


class CreateApiKeyData(ApiModel):
    """Response data returned when a new API key is created."""
    raw_key: str
    key: ApiKeyListItem


class ApiKeyListResp(BaseResponse):
    """GET /v1/internal/api-keys/"""

    data: Optional[list[ApiKeyListItem]] = None


class CreateApiKeyResp(BaseResponse):
    """POST /v1/internal/api-keys/ — raw_key appears here once and never again."""

    data: Optional[CreateApiKeyData] = None


class RevokeApiKeyResp(BaseResponse):
    """DELETE /v1/internal/api-keys/{key_id} — data is always null."""
