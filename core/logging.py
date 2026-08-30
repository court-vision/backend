"""Shim over cv_core.logging — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.logging import (  # noqa: F401
    LoggerAdapter,
    add_correlation_id,
    add_service_info,
    correlation_id_var,
    get_correlation_id,
    get_logger,
    sentry_processor,
    set_correlation_id,
    setup_logging,
)
