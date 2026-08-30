"""Shim over cv_core.correlation_middleware — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.correlation_middleware import (  # noqa: F401
    CORRELATION_HEADER,
    CorrelationMiddleware,
    ERROR_CODE_HEADER,
    QUIET_PATHS,
    RequestContextMiddleware,
    client_ip,
    incoming_correlation_id,
    route_template,
)
