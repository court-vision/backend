"""
API key model for authenticated API access.
"""

import hashlib
import secrets
import uuid
from datetime import datetime

from peewee import (
    UUIDField,
    CharField,
    IntegerField,
    DateTimeField,
    BooleanField,
    ForeignKeyField,
)
from playhouse.postgres_ext import ArrayField

from db.base import BaseModel
from db.models.users import User


class APIKey(BaseModel):
    """API key for programmatic access to protected endpoints."""

    id = UUIDField(primary_key=True, default=uuid.uuid4)
    user = ForeignKeyField(User, backref="api_keys", null=True, on_delete="SET NULL")
    key_hash = CharField(max_length=64, unique=True)
    key_prefix = CharField(max_length=11)  # For display: "cv_abc1..."
    name = CharField(max_length=100)
    scopes = ArrayField(CharField, default=[])  # ['read', 'optimize', 'admin']
    rate_limit = IntegerField(default=1000)  # Requests per minute
    created_at = DateTimeField(default=datetime.utcnow)
    expires_at = DateTimeField(null=True)
    last_used_at = DateTimeField(null=True)
    is_active = BooleanField(default=True)

    class Meta:
        table_name = "api_keys"
        schema = "usr"

    @classmethod
    def create_key(
        cls,
        name: str,
        scopes: list[str],
        user: User | None = None,
        rate_limit: int = 1000,
        expires_at: datetime | None = None,
    ) -> tuple[str, "APIKey"]:
        """
        Create a new API key.

        Returns:
            Tuple of (raw_key, api_key_record). The raw_key is only returned once
            and should be provided to the user immediately.
        """
        raw_key = f"cv_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        api_key = cls.create(
            user=user,
            key_hash=key_hash,
            key_prefix=raw_key[:11],
            name=name,
            scopes=scopes,
            rate_limit=rate_limit,
            expires_at=expires_at,
        )
        return raw_key, api_key

    @classmethod
    def verify_key(cls, raw_key: str) -> "APIKey | None":
        """
        Verify an API key and return the record if valid.

        Returns:
            APIKey record if valid, None if invalid or expired.
        """
        if not raw_key or not raw_key.startswith("cv_"):
            return None

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        try:
            api_key = cls.get(
                (cls.key_hash == key_hash) & (cls.is_active == True)  # noqa: E712
            )

            # Check expiration
            if api_key.expires_at and api_key.expires_at < datetime.utcnow():
                return None

            # Update last used timestamp
            api_key.last_used_at = datetime.utcnow()
            api_key.save()

            return api_key

        except cls.DoesNotExist:
            return None

    def has_scope(self, scope: str) -> bool:
        """Check if API key has a specific scope."""
        return scope in (self.scopes or [])

    def __repr__(self):
        return f"<APIKey(id={self.id}, name='{self.name}', prefix='{self.key_prefix}')>"
