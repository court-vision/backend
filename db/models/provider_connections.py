"""
Encrypted third-party provider credentials.

One row per (user, provider, provider-side account). The secrets live in
`secret_ciphertext` as a Fernet token over a JSON object; nothing here decrypts
on its own -- go through `services.credential_service` so every read is
deliberate and every failure is typed.

This model must never appear in an API response. `__repr__` and `__str__` are
overridden so an accidental log line or exception message cannot leak the
ciphertext.
"""

from datetime import datetime, timezone

from peewee import (
    CharField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    SmallIntegerField,
    TextField,
)

from db.base import BaseModel
from db.models.users import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderConnection(BaseModel):
    id = IntegerField(primary_key=True)
    user = ForeignKeyField(User, column_name="user_id", backref="provider_connections", on_delete="CASCADE")
    provider = CharField(max_length=16)

    # ESPN: the SWID guid. Yahoo: the account guid when known. Empty string
    # rather than NULL so the unique constraint constrains.
    external_account_id = CharField(max_length=128, default="")

    secret_ciphertext = TextField()
    key_version = SmallIntegerField()

    # Cleartext mirror of the Yahoo token expiry so refresh scheduling does not
    # need to decrypt. NULL for ESPN.
    expires_at = DateTimeField(null=True)

    created_at = DateTimeField(default=_utcnow)
    updated_at = DateTimeField(default=_utcnow)
    last_used_at = DateTimeField(null=True)

    class Meta:
        table_name = "provider_connections"
        schema = "usr"
        indexes = ((("user", "provider", "external_account_id"), True),)

    def save(self, *args, **kwargs):
        self.updated_at = _utcnow()
        return super().save(*args, **kwargs)

    # The ciphertext is not a secret in the way a plaintext token is, but a
    # model that holds credentials should be incapable of printing them --
    # repr() ends up in tracebacks, logs and Sentry breadcrumbs.
    def __repr__(self) -> str:
        return f"<ProviderConnection(id={self.id}, user_id={self.user_id}, provider={self.provider!r})>"

    __str__ = __repr__
