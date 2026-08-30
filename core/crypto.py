"""Shim over cv_core.crypto — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.

The one repo-specific line: cv_core reads CREDENTIAL_KEYS straight from the
environment by default, but this service's settings layer also loads .env
files, so the keyring is sourced through settings to keep that behavior.
"""

from cv_core import crypto as _crypto
from cv_core.crypto import (  # noqa: F401
    CredentialDecryptionError,
    current_version,
    decrypt,
    encrypt,
    is_enabled,
    reset_cache,
    set_keys_source,
)


def _settings_keys() -> str:
    from core.settings import settings

    return settings.credential_keys


_crypto.set_keys_source(_settings_keys)
