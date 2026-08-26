"""
`respond(resp)`: send a service envelope with the HTTP status its `status` implies.

Services return `BaseResponse` envelopes (`{status, message, data, error_code}`).
Routes wrap them: a SUCCESS envelope is returned unchanged (FastAPI still
applies the route's `response_model`, so the body is what it always was);
anything else becomes a `JSONResponse` whose HTTP status comes from the
`error_code` override table first, then from `status`. The body keeps the
envelope's own fields (`team_id`, `already_exists`, `meta`, ...), `error_code`
is filled in when the service left it empty, and the `X-Error-Code` header
feeds the `http_request` log line.

Rule: an empty state is a SUCCESS envelope with `data: null` / `[]` and a
`message` ("No current matchup found", "No 2026-27 data yet") -> 200. Only
failures get 4xx/5xx. A failure the service can name should be *raised* as a
`core.errors.AppError` (the handlers log it and stamp the correlation id);
`respond` covers envelopes that still carry their status themselves, such as
the lineup service's `LINEUP_SERVICE_*` codes and rankings' BAD_REQUEST.

`POST /teams/{id}/league/sync` is the one documented exception: it returns an
ERROR envelope *with data* as a soft warning ("provider settings unavailable,
using default points scoring") and stays a 200, so it is not wrapped.
"""

from __future__ import annotations

from typing import Union

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from core.logging import get_logger
from schemas.common import ApiStatus, BaseResponse

ERROR_CODE_HEADER = "X-Error-Code"

STATUS_MAP: dict[ApiStatus, int] = {
    ApiStatus.SUCCESS: 200,
    ApiStatus.BAD_REQUEST: 400,
    ApiStatus.AUTHENTICATION_ERROR: 401,
    ApiStatus.AUTHORIZATION_ERROR: 403,
    ApiStatus.NOT_FOUND: 404,
    ApiStatus.CONFLICT: 409,
    ApiStatus.VALIDATION_ERROR: 422,
    ApiStatus.RATE_LIMITED: 429,
    ApiStatus.SERVER_ERROR: 500,
    ApiStatus.ERROR: 500,
}

# An error_code pins the HTTP status regardless of the envelope's `status`
ERROR_CODE_STATUS: dict[str, int] = {
    "LINEUP_SERVICE_UNAVAILABLE": 503,
    "LINEUP_SERVICE_REJECTED": 422,
    "PROVIDER_UNAVAILABLE": 502,
    "PROVIDER_BAD_RESPONSE": 502,
    "PROVIDER_TIMEOUT": 504,
    "PROVIDER_AUTH_EXPIRED": 403,
    "LEAGUE_NOT_FOUND": 400,
    "LEAGUE_VALIDATION_FAILED": 400,
    "TEAM_NAME_NOT_IN_LEAGUE": 400,
    "TEAM_NOT_FOUND": 404,
    "PLAYER_NOT_FOUND": 404,
    "LINEUP_NOT_FOUND": 404,
    "LINEUP_ALREADY_EXISTS": 409,
}

# error_code to fill in when the service left it empty, by envelope status
DEFAULT_ERROR_CODES: dict[ApiStatus, str] = {
    ApiStatus.BAD_REQUEST: "BAD_REQUEST",
    ApiStatus.AUTHENTICATION_ERROR: "AUTH_REQUIRED",
    ApiStatus.AUTHORIZATION_ERROR: "FORBIDDEN",
    ApiStatus.NOT_FOUND: "NOT_FOUND",
    ApiStatus.CONFLICT: "CONFLICT",
    ApiStatus.VALIDATION_ERROR: "VALIDATION_ERROR",
    ApiStatus.RATE_LIMITED: "RATE_LIMITED",
    ApiStatus.SERVER_ERROR: "INTERNAL_ERROR",
    ApiStatus.ERROR: "INTERNAL_ERROR",
}


def http_status_for(resp: BaseResponse) -> int:
    """The HTTP status an envelope implies (200 for SUCCESS, whatever its data)."""
    status = ApiStatus(resp.status)
    if status is ApiStatus.SUCCESS:
        return 200
    if resp.error_code and resp.error_code in ERROR_CODE_STATUS:
        return ERROR_CODE_STATUS[resp.error_code]
    return STATUS_MAP.get(status, 500)


def respond(resp: BaseResponse) -> Union[BaseResponse, JSONResponse]:
    """Return `resp` as-is when it is a success; otherwise as a JSONResponse with the real status."""
    status_code = http_status_for(resp)
    if status_code < 400:
        return resp

    status = ApiStatus(resp.status)
    error_code = resp.error_code or DEFAULT_ERROR_CODES.get(status, "INTERNAL_ERROR")
    content = jsonable_encoder(resp)
    content["error_code"] = error_code
    if status_code >= 500:
        # A service answered with a bare failure envelope instead of raising: visible in the
        # logs (no stack trace exists to send to Sentry), and a nudge to make it an AppError.
        get_logger("http").error("error_envelope", status_code=status_code, api_status=status.value,
                                 error_code=error_code, message=resp.message)
    return JSONResponse(status_code=status_code, content=content, headers={ERROR_CODE_HEADER: error_code})
