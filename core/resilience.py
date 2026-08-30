"""Shim over cv_core.resilience — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.resilience import (  # noqa: F401
    ClientError,
    NetworkError,
    RateLimitError,
    ResilientHTTPClient,
    RetryableError,
    ServerError,
    T,
    classify_response_error,
    create_circuit_breaker,
    create_retry_decorator,
    espn_api_circuit,
    is_circuit_open,
    nba_api_circuit,
    resilient_request,
    with_retry,
)
