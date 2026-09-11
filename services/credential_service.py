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

A connection is also something the user manages directly
(`api/v1/internal/connections.py`): one refresh of an ESPN account's cookies
reaches every team on it, and a new team on that account reuses them. That
only holds if every spelling of one SWID is one row -- hence `normalize_swid`.

The one rule callers must respect: hydrate only where credentials are actually
needed (provider calls), never on the path that builds an API response.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
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

_SWID_NOISE = re.compile(r"[\s{}]")


def _provider_of(payload: dict) -> str:
    provider = payload.get("provider", "espn")
    return provider.value if hasattr(provider, "value") else str(provider)


def split_secrets(payload: dict) -> tuple[dict, dict]:
    """(payload without any credential field, just the credential fields present)."""
    fields = SECRET_FIELDS.get(_provider_of(payload), ())
    secrets = {k: payload[k] for k in fields if payload.get(k)}
    public = {k: v for k, v in payload.items() if k not in ALL_SECRET_FIELDS}
    return public, secrets


def normalize_swid(value: Optional[str]) -> str:
    """ESPN's SWID in its canonical form: an upper-case GUID in braces.

    ESPN issues the cookie as `{XXXXXXXX-XXXX-...}`; people paste it with or
    without the braces, in either case, with stray whitespace. It keys the
    connection row, so every spelling of one account must be one string. Empty
    stays empty. Migration 0024 applies the same rule to rows written before.
    """
    guid = _SWID_NOISE.sub("", value or "").upper()
    return "{" + guid + "}" if guid else ""


def _external_account_id(provider: str, secrets: dict) -> str:
    """The provider-side account a credential belongs to.

    ESPN's SWID is the account guid, so two leagues on one ESPN account collapse
    to a single connection. Yahoo exposes no comparable id here, so all of a
    user's Yahoo credentials share one row.
    """
    if provider == "espn":
        return normalize_swid(secrets.get("swid"))[:128]
    return ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _holds(connection, secrets: dict) -> bool:
    """Whether `connection` already stores exactly `secrets`."""
    try:
        return json.loads(crypto.decrypt(connection.secret_ciphertext, connection.key_version)) == secrets
    except crypto.CredentialDecryptionError:
        return False


def _fingerprint(connection) -> tuple[str, int]:
    """Exactly which credentials a row holds, for `mark_checked`."""
    return connection.secret_ciphertext, connection.key_version


def _find_connection(user_id: int, provider: str, account: str):
    from db.models.provider_connections import ProviderConnection

    return ProviderConnection.get_or_none(
        (ProviderConnection.user == user_id)
        & (ProviderConnection.provider == provider)
        & (ProviderConnection.external_account_id == account)
    )


def _upsert_connection(user_id: int, provider: str, secrets: dict, *, verified: bool = False) -> tuple[Any, bool]:
    """Create or update the row for this (user, provider, account); returns (row, created).

    The one write path into the store. Saving the secrets a row already holds
    changes nothing -- every team edit re-persists the credentials it merged
    from the store, and that is not a change of credentials. New secrets clear
    the verdicts, which were about the old ones; `verified=True` when the
    provider has just accepted these. Two requests for one account can both
    find no row: the one that loses the insert takes the update path rather
    than failing on the unique key.
    """
    from peewee import IntegrityError

    from db.base import db
    from db.models.provider_connections import ProviderConnection

    if provider == "espn" and secrets.get("swid"):
        secrets = {**secrets, "swid": normalize_swid(secrets["swid"])}
    account = _external_account_id(provider, secrets)

    connection = _find_connection(user_id, provider, account)
    if connection is not None and _holds(connection, secrets):
        if verified:
            mark_checked(connection.id, ok=True, fingerprint=_fingerprint(connection))
        return connection, False

    ciphertext, key_version = crypto.encrypt(json.dumps(secrets))
    fields = {
        "secret_ciphertext": ciphertext,
        "key_version": key_version,
        "expires_at": secrets.get("yahoo_token_expiry") or None,
        "verified_at": _utcnow() if verified else None,
        "auth_failed_at": None,
    }
    if connection is None:
        try:
            with db.atomic():
                connection = ProviderConnection.create(
                    user=user_id, provider=provider, external_account_id=account, **fields
                )
            return connection, True
        except IntegrityError:
            connection = _find_connection(user_id, provider, account)
            if connection is None:
                raise
    for name, value in fields.items():
        setattr(connection, name, value)
    connection.save()
    return connection, False


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
    connection, _ = _upsert_connection(user_id, provider, secrets)
    return connection.id


def store_espn_cookies(user_id: int, espn_s2: str, swid: str, *, verified: bool = True) -> tuple[int, bool]:
    """Save an ESPN cookie pair as that account's connection; returns (connection id, created).

    Keyed by the normalized SWID, so refreshing an account already connected
    updates its one row -- and with it every team linked to it. `verified` is
    whether ESPN confirmed the pair; connection_service passes False when the
    account had no private league to confirm it with, which leaves the status
    unknown. The caller checks that the store is enabled.
    """
    connection, created = _upsert_connection(
        user_id, "espn", {"espn_s2": espn_s2, "swid": swid}, verified=verified
    )
    return connection.id, created


def _owned_connection(user_id: int, connection_id: int, provider: Optional[str]):
    from db.models.provider_connections import ProviderConnection

    where = (ProviderConnection.id == connection_id) & (ProviderConnection.user == user_id)
    if provider is not None:
        where &= ProviderConnection.provider == provider
    return ProviderConnection.get_or_none(where)


def load_provider_tokens(user_id: int, connection_id: int, provider: Optional[str] = None) -> Optional[dict]:
    """Decrypt a connection's secrets, but only for the user who owns it.

    The user scoping is the access control: a connection id is a small integer,
    so it must never be usable by anyone other than its owner. `provider`
    narrows it further, so an ESPN handle cannot resolve to a Yahoo row. With
    the store off there is nothing to load.
    """
    loaded = load_with_fingerprint(user_id, connection_id, provider)
    return loaded[0] if loaded else None


def load_with_fingerprint(
    user_id: int, connection_id: int, provider: Optional[str] = None
) -> Optional[tuple[dict, tuple[str, int]]]:
    """`load_provider_tokens`, plus a fingerprint of exactly what was loaded.

    A check hands the fingerprint back to `mark_checked`, so its verdict lands
    only on the credentials it was made with -- not on a pair saved while it
    was running.
    """
    if not crypto.is_enabled():
        return None
    connection = _owned_connection(user_id, connection_id, provider)
    if connection is None:
        return None
    secrets = json.loads(crypto.decrypt(connection.secret_ciphertext, connection.key_version))
    return secrets, _fingerprint(connection)


def connection_ids(user_id: int, provider: str) -> list[int]:
    """The user's connections for one provider, most recently written first.

    Answers from the rows alone -- nothing is decrypted -- and is empty with
    the store off.
    """
    if not crypto.is_enabled():
        return []

    from db.models.provider_connections import ProviderConnection

    rows = (
        ProviderConnection.select(ProviderConnection.id)
        .where((ProviderConnection.user == user_id) & (ProviderConnection.provider == provider))
        .order_by(ProviderConnection.updated_at.desc())
    )
    return [row.id for row in rows]


def mark_checked(connection_id: int, ok: bool, *, fingerprint: tuple[str, int]) -> bool:
    """Record the provider's verdict on a connection's credentials; False when not recorded.

    Recorded only if the row still holds the credentials the check was made
    with (`fingerprint`, from load_with_fingerprint): a check of the old pair
    must not stamp a pair saved while it ran. Leaves `updated_at` alone on
    purpose: it means "credentials last written", which is what migration 0024
    went by to keep the newest of two duplicates.
    """
    from db.models.provider_connections import ProviderConnection

    now = _utcnow()
    if ok:
        fields = {ProviderConnection.verified_at: now, ProviderConnection.auth_failed_at: None}
    else:
        fields = {ProviderConnection.auth_failed_at: now}
    ciphertext, key_version = fingerprint
    updated = (
        ProviderConnection.update(fields)
        .where(
            (ProviderConnection.id == connection_id)
            & (ProviderConnection.secret_ciphertext == ciphertext)
            & (ProviderConnection.key_version == key_version)
        )
        .execute()
    )
    return updated > 0


def delete_connection(user_id: int, connection_id: int) -> Optional[list[int]]:
    """Delete one of the user's connections; returns the teams it leaves unlinked.

    None when the user has no such connection. The teams themselves stay:
    `usr.teams.provider_connection_id` is ON DELETE SET NULL, so a public
    league keeps working and a private one asks for cookies again.
    """
    from db.models.provider_connections import ProviderConnection
    from db.models.teams import Team

    connection = ProviderConnection.get_or_none(
        (ProviderConnection.id == connection_id) & (ProviderConnection.user == user_id)
    )
    if connection is None:
        return None
    team_ids = [
        team.team_id
        for team in Team.select(Team.team_id).where(Team.provider_connection == connection.id)
    ]
    connection.delete_instance()
    return team_ids


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

    Half an ESPN pair is dropped rather than stored: it authenticates nothing,
    and writing it would replace the whole pair for every team on the account.
    """
    if not crypto.is_enabled():
        return None

    public, secrets = split_secrets(payload)
    if not secrets:
        return team.provider_connection_id

    provider = _provider_of(payload)
    if provider == "espn" and not (secrets.get("espn_s2") and secrets.get("swid")):
        log.warning("espn_partial_credentials_dropped", team_id=getattr(team, "team_id", None))
        team.league_info = json.dumps(public)
        team.save()
        return team.provider_connection_id

    connection, _ = _upsert_connection(user_id, provider, secrets)
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
