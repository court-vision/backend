"""Shim over cv_core.errors — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.errors import (  # noqa: F401
    AppError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ConflictError,
    DATABASE_UNAVAILABLE_CODE,
    DatabaseUnavailableError,
    INTERNAL_ERROR_CODE,
    NotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    RATE_LIMITED_CODE,
    ServiceUnavailableError,
    VALIDATION_ERROR_CODE,
    api_status_for,
)
