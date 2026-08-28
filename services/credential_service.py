"""
Where provider credentials are read from and written to.

Credentials used to live in `usr.teams.league_info` as plaintext JSON alongside
non-secret fields. They now live encrypted in `usr.provider_connections`, keyed
by (user, provider, provider-side account) -- one row per real account instead
of one copy per team.

Every path is dual-mode so the migration can be deployed before it is completed:

    CREDENTIAL_KEYS unset    -> the store is off; secrets stay in league_info
                                exactly as before (local dev, tests, and the
                                deploy that precedes setting the variable)
    set, team unlinked       -> reads fall back to league_info, writes create a
                                connection and strip the secrets from the JSON
    set, team linked         -> reads decrypt, writes update the connection

So a team is migrated the first time it is written, and `scripts/backfill_provider_connections.py`
moves the rest. Nothing has to happen in a particular order.

The one rule callers must respect: hydrate only where credentials are actually
needed (provider calls), never on the path that builds an API response.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from core import crypto
from core.logging import get_logger

log = get_logger("credentials")

# The fields that are credentials, per provider. Everything else in league_info
# (league_id, team_name, year, scoring_preview, yahoo_team_key) is not secret
# and stays where it is.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "espn": ("espn_s2", "swid"),
    "yahoo": ("yahoo_access_token", "yahoo_refresh_token", "yahoo_token_expiry"),
}

ALL_SECRET_FIELDS: frozenset[str] = frozenset(f for fields in SECRET_FIELDS.values() for f in fields)


def _provider_of(payload: dict) -> str:
    provider = payload.get("provider", "espn")
    return provider.value if hasattr(provider, "value") else str(provider)


def split_secrets(payload: dict) -> tuple[dict, dict]:
    """(payload without any credential field, just the credential fields present)."""
    fields = SECRET_FIELDS.get(_provider_of(payload), ())
    secrets = {k: payload[k] for k in fields if payload.get(k)}
    public = {k: v for k, v in payload.items() if k not in ALL_SECRET_FIELDS}
    return public, secrets


def _external_account_id(provider: str, secrets: dict) -> str:
    """The provider-side account a credential belongs to.

    ESPN's SWID is the account guid, so two leagues on one ESPN account collapse
    to a single connection. Yahoo exposes no comparable id here, so all of a
    user's Yahoo credentials share one row.
    """
    if provider == "espn":
        return (secrets.get("swid") or "")[:128]
    return ""


def store_provider_tokens(user_id: int, provider: str, secrets: dict) -> Optional[int]:
    """Put freshly-obtained credentials straight into the encrypted store.

    Used by the OAuth callback so tokens never travel to the browser. The
    returned connection id is an opaque, user-scoped handle: it identifies a row
    the caller already owns and grants nothing on its own, unlike the tokens it
    replaces in the redirect URL.

    Returns None when the store is disabled, in which case the caller must fall
    back to its previous behaviour.
    """
    if not crypto.is_enabled() or not secrets:
        return None

    from db.models.provider_connections import ProviderConnection

    account = _external_account_id(provider, secrets)
    ciphertext, key_version = crypto.encrypt(json.dumps(secrets))
    expires_at = secrets.get("yahoo_token_expiry") or None

    connection = ProviderConnection.get_or_none(
        (ProviderConnection.user == user_id)
        & (ProviderConnection.provider == provider)
        & (ProviderConnection.external_account_id == account)
    )
    if connection is None:
        connection = ProviderConnection.create(
            user=user_id, provider=provider, external_account_id=account,
            secret_ciphertext=ciphertext, key_version=key_version, expires_at=expires_at,
        )
    else:
        connection.secret_ciphertext = ciphertext
        connection.key_version = key_version
        connection.expires_at = expires_at
        connection.save()
    return connection.id


def load_provider_tokens(user_id: int, connection_id: int) -> Optional[dict]:
    """Decrypt a connection's secrets, but only for the user who owns it.

    The user scoping is the access control: a connection id is a small integer,
    so it must never be usable by anyone other than its owner.
    """
    from db.models.provider_connections import ProviderConnection

    connection = ProviderConnection.get_or_none(
        (ProviderConnection.id == connection_id) & (ProviderConnection.user == user_id)
    )
    if connection is None:
        return None
    return json.loads(crypto.decrypt(connection.secret_ciphertext, connection.key_version))


def has_credentials(team, payload: dict) -> bool:
    """Whether this team has credentials on file — without decrypting them.

    The client-facing response needs to say "stored" without ever touching the
    plaintext, so this answers from the connection link for migrated teams and
    from the legacy JSON for the rest.
    """
    if getattr(team, "provider_connection_id", None):
        return True
    _, secrets = split_secrets(payload)
    return bool(secrets)


def persist(user_id: int, team, payload: dict) -> Optional[int]:
    """Move the credentials in `payload` into the encrypted store.

    Returns the connection id, or None when the store is disabled. `team` is
    updated in place and saved: `league_info` loses its secrets and
    `provider_connection_id` gains the link.
    """
    if not crypto.is_enabled():
        return None

    public, secrets = split_secrets(payload)
    if not secrets:
        return team.provider_connection_id

    from db.models.provider_connections import ProviderConnection

    provider = _provider_of(payload)
    account = _external_account_id(provider, secrets)
    ciphertext, key_version = crypto.encrypt(json.dumps(secrets))
    expires_at = secrets.get("yahoo_token_expiry") or None

    connection = ProviderConnection.get_or_none(
        (ProviderConnection.user == user_id)
        & (ProviderConnection.provider == provider)
        & (ProviderConnection.external_account_id == account)
    )
    if connection is None:
        connection = ProviderConnection.create(
            user=user_id, provider=provider, external_account_id=account,
            secret_ciphertext=ciphertext, key_version=key_version, expires_at=expires_at,
        )
    else:
        connection.secret_ciphertext = ciphertext
        connection.key_version = key_version
        connection.expires_at = expires_at
        connection.save()

    team.provider_connection_id = connection.id
    team.league_info = json.dumps(public)
    team.save()
    return connection.id


def hydrate(team, payload: dict) -> dict:
    """Merge this team's stored credentials into `payload`.

    Only call this where the credentials are about to be used. A team with no
    connection is returned unchanged -- its secrets are still in league_info.
    """
    connection_id = getattr(team, "provider_connection_id", None)
    if not connection_id or not crypto.is_enabled():
        return payload

    from db.models.provider_connections import ProviderConnection

    connection = ProviderConnection.get_or_none(ProviderConnection.id == connection_id)
    if connection is None:
        log.warning("provider_connection_missing", team_id=getattr(team, "team_id", None),
                    connection_id=connection_id)
        return payload

    try:
        secrets = json.loads(crypto.decrypt(connection.secret_ciphertext, connection.key_version))
    except crypto.CredentialDecryptionError:
        # Loud, and without the ciphertext: a key that cannot decrypt its own
        # rows is an operational error, not a per-request one.
        log.error("credential_decrypt_failed", team_id=getattr(team, "team_id", None),
                  connection_id=connection_id, key_version=connection.key_version)
        raise

    return {**payload, **secrets}


def update_yahoo_tokens(team, access_token: str, refresh_token: str, token_expiry: str) -> bool:
    """Persist refreshed Yahoo tokens wherever this team's credentials live.

    Returns True when the write landed in the encrypted store, False when the
    caller should fall back to rewriting league_info.
    """
    if not (crypto.is_enabled() and getattr(team, "provider_connection_id", None)):
        return False

    from db.models.provider_connections import ProviderConnection

    connection = ProviderConnection.get_or_none(ProviderConnection.id == team.provider_connection_id)
    if connection is None:
        return False

    secrets = {
        "yahoo_access_token": access_token,
        "yahoo_refresh_token": refresh_token,
        "yahoo_token_expiry": token_expiry,
    }
    connection.secret_ciphertext, connection.key_version = crypto.encrypt(json.dumps(secrets))
    connection.expires_at = token_expiry or None
    connection.save()
    return True
