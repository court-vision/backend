"""
Request context middleware.

One middleware, outermost in the stack, that:

- reads `X-Correlation-ID` from the request (or generates a UUID), exposes it
  to logs (`core.logging` contextvar + structlog contextvars), to Sentry (tag)
  and to the response header;
- binds `method` / `path` to every log line emitted during the request;
- logs exactly one `http_request` line per request (INFO; DEBUG for `/health`,
  `/ping`, `/`) with the matched route template, status, duration, client IP,
  user agent, `error_code` (from the `X-Error-Code` response header) and
  `user_id` when a dependency resolved one (`request.state.user_id`).

Unhandled exceptions cannot be turned into a response here — Starlette's
`ServerErrorMiddleware` sits outside us and renders the registered `Exception`
handler — so the 500 line is logged from the `except` and the exception is
re-raised; the handler itself stamps `X-Correlation-ID` on the 500 body.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import sentry_sdk
import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from core.errors import INTERNAL_ERROR_CODE
from core.logging import get_logger, set_correlation_id

CORRELATION_HEADER = "X-Correlation-ID"
ERROR_CODE_HEADER = "X-Error-Code"

# Health/liveness pollers would otherwise dominate the request log.
QUIET_PATHS = frozenset({"/health", "/ping", "/"})

_MAX_CORRELATION_ID_LEN = 128
_MAX_USER_AGENT_LEN = 120


def incoming_correlation_id(request: Request) -> str:
    """The caller's id when it is sane, else a fresh UUID."""
    cid = (request.headers.get(CORRELATION_HEADER) or "").strip()
    if cid and len(cid) <= _MAX_CORRELATION_ID_LEN and cid.isprintable():
        return cid
    return str(uuid.uuid4())


def route_template(request: Request) -> Optional[str]:
    """The matched route's path template (`/v1/players/{player_id}/stats`), if any."""
    route = request.scope.get("route")
    if route is not None:
        return getattr(route, "path", None)
    app = request.scope.get("app")
    router = getattr(app, "router", None)
    if router is None:
        return None
    for candidate in router.routes:
        try:
            match, _ = candidate.matches(request.scope)
        except Exception:  # a route type without `matches`
            continue
        if match == Match.FULL:
            return getattr(candidate, "path", None)
    return None


def client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        cid = incoming_correlation_id(request)
        set_correlation_id(cid)
        request.state.correlation_id = cid

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=cid,
            method=request.method,
            path=request.url.path,
        )
        if sentry_sdk.is_initialized():
            sentry_sdk.set_tag("correlation_id", cid)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log_request(request, cid, 500, started, INTERNAL_ERROR_CODE)
            raise

        response.headers[CORRELATION_HEADER] = cid
        self._log_request(
            request, cid, response.status_code, started, response.headers.get(ERROR_CODE_HEADER)
        )
        return response

    @staticmethod
    def _log_request(
        request: Request,
        cid: str,
        status_code: int,
        started: float,
        error_code: Optional[str],
    ) -> None:
        log = get_logger("http")
        fields = {
            "correlation_id": cid,
            "method": request.method,
            "path": request.url.path,
            "route": route_template(request),
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "client_ip": client_ip(request),
            "user_agent": (request.headers.get("user-agent") or "")[:_MAX_USER_AGENT_LEN] or None,
        }
        if error_code:
            fields["error_code"] = error_code
        user_id = getattr(request.state, "user_id", None)
        if user_id is not None:
            fields["user_id"] = user_id
        if request.url.path in QUIET_PATHS:
            log.debug("http_request", **fields)
        else:
            log.info("http_request", **fields)


# Backwards-compatible name: the old middleware only did the correlation id part.
CorrelationMiddleware = RequestContextMiddleware
