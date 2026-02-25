"""
API Key management routes.

Authenticated users can create, list, and revoke their API keys.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from core.clerk_auth import verify_clerk_token
from db.models.api_keys import APIKey
from db.models.users import User
from schemas.api_keys import ApiKeyListItem, CreateApiKeyRequest, CreateApiKeyData
from schemas.common import success_response

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


def _get_user(token_data: dict) -> User:
    """Look up the local User record from a Clerk token."""
    clerk_user_id = token_data.get("clerk_user_id")
    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Invalid token: missing user ID")
    try:
        return User.get(User.clerk_user_id == clerk_user_id)
    except User.DoesNotExist:
        raise HTTPException(status_code=404, detail="User not found")


def _key_to_item(key: APIKey) -> dict:
    """Convert an APIKey model instance to an ApiKeyListItem dict."""
    return ApiKeyListItem(
        id=str(key.id),
        name=key.name,
        key_prefix=key.key_prefix,
        scopes=list(key.scopes or []),
        rate_limit=key.rate_limit,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        is_active=key.is_active,
    ).model_dump(mode="json")


@router.get("/")
async def list_api_keys(token_data: dict = Depends(verify_clerk_token)):
    """List all active API keys for the authenticated user."""
    user = _get_user(token_data)

    keys = (
        APIKey.select()
        .where((APIKey.user == user) & (APIKey.is_active == True))  # noqa: E712
        .order_by(APIKey.created_at.desc())
    )

    return success_response(
        message="API keys retrieved",
        data=[_key_to_item(k) for k in keys],
    )


@router.post("/")
async def create_api_key(
    body: CreateApiKeyRequest,
    token_data: dict = Depends(verify_clerk_token),
):
    """
    Create a new API key. The raw key is returned only once in this response.
    """
    user = _get_user(token_data)

    expires_at = None
    if body.expires_days is not None:
        expires_at = datetime.utcnow() + timedelta(days=body.expires_days)

    raw_key, api_key = APIKey.create_key(
        name=body.name,
        scopes=body.scopes,
        user=user,
        rate_limit=1000,
        expires_at=expires_at,
    )

    data = CreateApiKeyData(
        raw_key=raw_key,
        key=ApiKeyListItem(
            id=str(api_key.id),
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            scopes=list(api_key.scopes or []),
            rate_limit=api_key.rate_limit,
            created_at=api_key.created_at,
            last_used_at=api_key.last_used_at,
            expires_at=api_key.expires_at,
            is_active=api_key.is_active,
        ),
    ).model_dump(mode="json")

    return success_response(
        message="API key created. Store the raw_key now — it cannot be retrieved again.",
        data=data,
    )


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    token_data: dict = Depends(verify_clerk_token),
):
    """Soft-delete an API key (set is_active=False)."""
    user = _get_user(token_data)

    try:
        api_key = APIKey.get(APIKey.id == key_id)
    except APIKey.DoesNotExist:
        raise HTTPException(status_code=404, detail="API key not found")

    # Verify ownership
    if api_key.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = False
    api_key.save()

    return success_response(message="API key revoked")
