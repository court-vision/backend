"""
Application error taxonomy.

Every error the API returns renders through one envelope
(`schemas.common.error_response`: `{status, message, data, error_code}`).
Raise one of these anywhere in a request (route, dependency, service) and
`core.middleware.render_error` turns it into the right HTTP status, keeps the
body free of exception text, and stamps `X-Correlation-ID` / `X-Error-Code`.

| Exception                | HTTP | status               | default error_code     |
|--------------------------|------|----------------------|------------------------|
| BadRequestError          | 400  | bad_request          | BAD_REQUEST            |
| AuthenticationError      | 401  | authentication_error | AUTH_REQUIRED          |
| AuthorizationError       | 403  | authorization_error  | FORBIDDEN              |
| ProviderAuthError        | 403  | authorization_error  | PROVIDER_AUTH_EXPIRED  |
| NotFoundError            | 404  | not_found            | NOT_FOUND              |
| ConflictError            | 409  | conflict             | CONFLICT               |
| ProviderError            | 502  | server_error         | PROVIDER_UNAVAILABLE   |
| ServiceUnavailableError  | 503  | server_error         | SERVICE_UNAVAILABLE    |
| DatabaseUnavailableError | 503  | server_error         | DATABASE_UNAVAILABLE   |
| ProviderTimeout          | 504  | server_error         | PROVIDER_TIMEOUT       |

Anything that is not an `AppError` (or an `HTTPException` / validation /
peewee error, which the handlers map themselves) is a 500 `INTERNAL_ERROR`.
"""

from __future__ import annotations

from typing import Any, Optional

from schemas.common import ApiStatus

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
DATABASE_UNAVAILABLE_CODE = "DATABASE_UNAVAILABLE"
VALIDATION_ERROR_CODE = "VALIDATION_ERROR"
RATE_LIMITED_CODE = "RATE_LIMITED"

_STATUS_BY_HTTP = {
    400: ApiStatus.BAD_REQUEST,
    401: ApiStatus.AUTHENTICATION_ERROR,
    403: ApiStatus.AUTHORIZATION_ERROR,
    404: ApiStatus.NOT_FOUND,
    409: ApiStatus.CONFLICT,
    422: ApiStatus.VALIDATION_ERROR,
    429: ApiStatus.RATE_LIMITED,
}


def api_status_for(status_code: int) -> ApiStatus:
    """The envelope `status` for an HTTP status code (used for residual HTTPExceptions)."""
    if status_code in _STATUS_BY_HTTP:
        return _STATUS_BY_HTTP[status_code]
    if status_code >= 500:
        return ApiStatus.SERVER_ERROR
    return ApiStatus.ERROR


class AppError(Exception):
    """Base class for errors that map to a specific HTTP status and `error_code`.

    Subclasses set the class attributes; instances may override any of them:

        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
        raise ProviderAuthError("yahoo")
        raise AppError("SOMETHING", "message", status_code=418)

    `message` is what the client sees (never raw exception text). `data` is
    merged into the envelope's `data` next to `correlation_id`. `log_level`
    is how `render_error` logs the error ("info" for expected client errors,
    "error" for 5xx, which also reaches Sentry).
    """

    status_code: int = 500
    api_status: ApiStatus = ApiStatus.SERVER_ERROR
    error_code: str = INTERNAL_ERROR_CODE
    default_message: str = "Something went wrong"
    log_level: str = "error"

    def __init__(
        self,
        error_code: Optional[str] = None,
        message: Optional[str] = None,
        *,
        data: Optional[dict[str, Any]] = None,
        status_code: Optional[int] = None,
        api_status: Optional[ApiStatus] = None,
        log_level: Optional[str] = None,
    ):
        if error_code:
            self.error_code = error_code
        self.message = message or self.default_message
        self.data = data
        if status_code is not None:
            self.status_code = status_code
            if api_status is None:
                api_status = api_status_for(status_code)
        if api_status is not None:
            self.api_status = api_status
        if log_level:
            self.log_level = log_level
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.error_code!r}, {self.message!r}, status_code={self.status_code})"


# ---- 4xx: the client (or its provider credentials) is at fault ---------------


class BadRequestError(AppError):
    status_code = 400
    api_status = ApiStatus.BAD_REQUEST
    error_code = "BAD_REQUEST"
    default_message = "Bad request"
    log_level = "info"


class AuthenticationError(AppError):
    """No usable identity: missing/expired/invalid Clerk token or API key."""

    status_code = 401
    api_status = ApiStatus.AUTHENTICATION_ERROR
    error_code = "AUTH_REQUIRED"
    default_message = "Authentication required"
    log_level = "info"


class AuthorizationError(AppError):
    status_code = 403
    api_status = ApiStatus.AUTHORIZATION_ERROR
    error_code = "FORBIDDEN"
    default_message = "Forbidden"
    log_level = "info"


class ProviderAuthError(AppError):
    """The user's ESPN cookies / Yahoo tokens were rejected by the provider.

    403 (not 401) so the frontend never mistakes it for a dead Clerk session;
    `data.provider` tells it which connection to redo.
    """

    status_code = 403
    api_status = ApiStatus.AUTHORIZATION_ERROR
    error_code = "PROVIDER_AUTH_EXPIRED"
    default_message = "Your league connection has expired — reconnect it in Manage Teams"
    log_level = "info"

    def __init__(self, provider: str, message: Optional[str] = None, **kwargs: Any):
        data = dict(kwargs.pop("data", None) or {})
        data["provider"] = provider
        super().__init__(None, message, data=data, **kwargs)
        self.provider = provider


class NotFoundError(AppError):
    status_code = 404
    api_status = ApiStatus.NOT_FOUND
    error_code = "NOT_FOUND"
    default_message = "Not found"
    log_level = "info"


class ConflictError(AppError):
    status_code = 409
    api_status = ApiStatus.CONFLICT
    error_code = "CONFLICT"
    default_message = "Conflict"
    log_level = "info"


# ---- 5xx: something on our side or upstream ----------------------------------


class ProviderError(AppError):
    """A provider (ESPN/Yahoo/NBA) answered 4xx/5xx or an unparseable body."""

    status_code = 502
    api_status = ApiStatus.SERVER_ERROR
    error_code = "PROVIDER_UNAVAILABLE"
    default_message = "The provider isn't responding — retry in a minute"
    log_level = "warning"

    def __init__(self, provider: str, message: Optional[str] = None, error_code: Optional[str] = None, **kwargs: Any):
        data = dict(kwargs.pop("data", None) or {})
        data["provider"] = provider
        super().__init__(error_code, message, data=data, **kwargs)
        self.provider = provider


class ProviderTimeout(ProviderError):
    status_code = 504
    error_code = "PROVIDER_TIMEOUT"
    default_message = "The provider timed out — retry in a minute"


class ServiceUnavailableError(AppError):
    """A dependency we need (auth keys, user directory, ...) is unavailable."""

    status_code = 503
    api_status = ApiStatus.SERVER_ERROR
    error_code = "SERVICE_UNAVAILABLE"
    default_message = "Service temporarily unavailable"
    log_level = "error"


class DatabaseUnavailableError(ServiceUnavailableError):
    error_code = DATABASE_UNAVAILABLE_CODE
    default_message = "Database temporarily unavailable"
