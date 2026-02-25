"""
Pydantic schemas for API key management endpoints.
"""

from datetime import datetime

from pydantic import BaseModel


class ApiKeyListItem(BaseModel):
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


class CreateApiKeyRequest(BaseModel):
    """Request body for creating a new API key."""
    name: str
    scopes: list[str] = ["read"]
    expires_days: int | None = None


class CreateApiKeyData(BaseModel):
    """Response data returned when a new API key is created."""
    raw_key: str
    key: ApiKeyListItem
