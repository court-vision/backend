"""
Envelope encryption for stored provider credentials.

ESPN `espn_s2`/`SWID` cookies and Yahoo refresh tokens are long-lived bearer
credentials for a user's account at a third party. They are stored so pipelines
can act on a user's behalf while they are asleep, which means they cannot be
hashed -- they have to come back out. Encrypting them at rest is what bounds the
damage when something else goes wrong, as it did on 2026-08-27 when an
unauthenticated query endpoint could read `usr.teams` as a superuser.

Keys live in the `CREDENTIAL_KEYS` environment variable as a comma-separated
list of `version:key` pairs, newest last:

    CREDENTIAL_KEYS=1:<fernet-key>,2:<fernet-key>

Each key is a urlsafe-base64 32-byte Fernet key; generate one with

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation: append a new higher version, redeploy, then re-encrypt existing rows
(scripts/rotate_credential_keys.py). Old versions must stay listed until nothing
references them -- `SELECT DISTINCT key_version FROM usr.provider_connections`
says when a key can be dropped.

When `CREDENTIAL_KEYS` is unset the store is *disabled* rather than broken:
`is_enabled()` returns False and callers fall back to the legacy plaintext
column. That keeps local development and the test suite working without a key,
and makes the production rollout a variable change rather than a code change.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.logging import get_logger

log = get_logger("crypto")


class CredentialDecryptionError(Exception):
    """Ciphertext could not be decrypted with the key its version names."""


@lru_cache(maxsize=1)
def _keys() -> dict[int, Fernet]:
    """Parse CREDENTIAL_KEYS into {version: Fernet}. Cached; process restart to reload."""
    from core.settings import settings

    raw = settings.credential_keys
    if not raw:
        return {}

    keys: dict[int, Fernet] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        version, _, key = entry.partition(":")
        if not key:
            raise ValueError("CREDENTIAL_KEYS entries must look like '1:<fernet-key>'")
        try:
            keys[int(version)] = Fernet(key.encode())
        except ValueError as exc:
            raise ValueError(f"CREDENTIAL_KEYS version {version!r} is not a valid Fernet key") from exc
    return keys


def is_enabled() -> bool:
    """True when at least one key is configured."""
    return bool(_keys())


def current_version() -> Optional[int]:
    """The version new ciphertext is written with: the highest configured."""
    keys = _keys()
    return max(keys) if keys else None


def encrypt(plaintext: str) -> tuple[str, int]:
    """Encrypt with the current key. Returns (ciphertext, key_version)."""
    keys = _keys()
    if not keys:
        raise RuntimeError("CREDENTIAL_KEYS is not configured; cannot encrypt")
    version = max(keys)
    return keys[version].encrypt(plaintext.encode()).decode(), version


def decrypt(ciphertext: str, key_version: int) -> str:
    """Decrypt a value written under `key_version`.

    Raises CredentialDecryptionError rather than returning a partial result: a
    caller that cannot read a credential must fail loudly, not silently act as
    though the user has no connection.
    """
    keys = _keys()
    fernet = keys.get(key_version)
    if fernet is None:
        raise CredentialDecryptionError(
            f"No key configured for version {key_version}; it must stay in CREDENTIAL_KEYS "
            f"until every row referencing it has been re-encrypted"
        )
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(f"Ciphertext failed authentication under key {key_version}") from exc


def reset_cache() -> None:
    """Drop the parsed-key cache. For tests that set CREDENTIAL_KEYS at runtime."""
    _keys.cache_clear()
