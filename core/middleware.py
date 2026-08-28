"""
Exception handlers and CORS.

`render_error(request, exc)` is the one place an exception becomes an HTTP
response. It classifies:

- `AppError` (core.errors)             -> its status / api_status / error_code
- `HTTPException` (residual FastAPI)   -> as raised, `error_code = HTTP_{code}`
- `RequestValidationError`             -> 422 VALIDATION_ERROR, `data.errors`
- peewee `OperationalError`/`InterfaceError` -> 503 DATABASE_UNAVAILABLE
- anything else                        -> 500 INTERNAL_ERROR, logged with the
  stack trace; the body never contains exception text

Every error body is the standard envelope with `data.correlation_id`, and every
error response carries `X-Correlation-ID` and `X-Error-Code` headers (the CORS
config exposes both to browsers).

Peewee connections are opened and released inside `db.base.run_db` workers;
the event-loop-side error handler only renders the stable response contract.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from peewee import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.errors import (
    DATABASE_UNAVAILABLE_CODE,
    INTERNAL_ERROR_CODE,
    VALIDATION_ERROR_CODE,
    AppError,
    api_status_for,
)
from core.logging import get_correlation_id, get_logger
from schemas.common import ApiStatus, error_response

CORRELATION_HEADER = "X-Correlation-ID"
ERROR_CODE_HEADER = "X-Error-Code"

DB_ERRORS = (OperationalError, InterfaceError)

_LOG_METHODS = {"debug", "info", "warning", "error", "critical"}


def correlation_id_for(request: Request) -> str:
    """The request's correlation id from state, context, header — or a new one."""
    state = request.scope.get("state") or {}
    cid = state.get("correlation_id") or get_correlation_id() or request.headers.get(CORRELATION_HEADER)
    return cid or str(uuid.uuid4())


def error_json_response(
    request: Request,
    *,
    status_code: int,
    api_status: ApiStatus,
    error_code: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    """The standard error envelope plus the correlation / error-code headers."""
    cid = correlation_id_for(request)
    payload = dict(data or {})
    payload["correlation_id"] = cid
    response_headers = dict(headers or {})
    response_headers[CORRELATION_HEADER] = cid
    response_headers[ERROR_CODE_HEADER] = error_code
    return JSONResponse(
        status_code=status_code,
        content=error_response(
            message=message,
            status=api_status,
            error_code=error_code,
            data=jsonable_encoder(payload),
        ),
        headers=response_headers,
    )


def render_error(request: Request, exc: BaseException) -> JSONResponse:
    log = get_logger("http")
    path = request.url.path

    if isinstance(exc, AppError):
        level = exc.log_level if exc.log_level in _LOG_METHODS else "error"
        getattr(log, level)(
            "app_error",
            error_code=exc.error_code,
            status_code=exc.status_code,
            path=path,
            message=exc.message,
            exc_info=exc if exc.status_code >= 500 else None,
        )
        return error_json_response(
            request,
            status_code=exc.status_code,
            api_status=exc.api_status,
            error_code=exc.error_code,
            message=exc.message,
            data=exc.data,
        )

    if isinstance(exc, RequestValidationError):
        errors = jsonable_encoder(exc.errors())
        log.info("validation_error", path=path, errors=errors)
        return error_json_response(
            request,
            status_code=422,
            api_status=ApiStatus.VALIDATION_ERROR,
            error_code=VALIDATION_ERROR_CODE,
            message="Request validation failed",
            data={"errors": errors},
        )

    if isinstance(exc, DB_ERRORS):
        log.exception("database_unavailable", path=path, error=type(exc).__name__)
        return error_json_response(
            request,
            status_code=503,
            api_status=ApiStatus.SERVER_ERROR,
            error_code=DATABASE_UNAVAILABLE_CODE,
            message="Database temporarily unavailable",
        )

    if isinstance(exc, StarletteHTTPException):
        status_code = exc.status_code
        detail = exc.detail
        if isinstance(detail, str) and detail:
            message, data = detail, None
        else:
            message, data = f"HTTP {status_code}", ({"detail": detail} if detail is not None else None)
        log_method = log.warning if status_code >= 500 else log.info
        log_method("http_exception", status_code=status_code, path=path, message=message)
        return error_json_response(
            request,
            status_code=status_code,
            api_status=api_status_for(status_code),
            error_code=f"HTTP_{status_code}",
            message=message,
            data=data,
            headers=exc.headers,
        )

    log.exception("unhandled_error", path=path, error=type(exc).__name__)
    return error_json_response(
        request,
        status_code=500,
        api_status=ApiStatus.SERVER_ERROR,
        error_code=INTERNAL_ERROR_CODE,
        message="Something went wrong on our side",
    )


async def handle_error(request: Request, exc: Exception) -> JSONResponse:
    return render_error(request, exc)


ALLOWED_ORIGINS = [
    "http://localhost:3000",  # Frontend
    "http://localhost:8080",  # Features server
    "https://www.courtvisionaries.live",  # Production
    "https://courtvisionaries.live",  # Production
    "https://www.courtvision.dev",  # Production
    "https://courtvision.dev",  # Production
    "https://sqlmate.courtvision.dev",  # SQLMate
]


def setup_middleware(app: FastAPI) -> None:
    """Register the exception handlers and CORS."""
    for exc_class in (AppError, StarletteHTTPException, RequestValidationError, OperationalError, InterfaceError, Exception):
        app.add_exception_handler(exc_class, handle_error)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER, ERROR_CODE_HEADER],
    )
