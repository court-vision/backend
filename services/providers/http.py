"""
The one HTTP layer for provider calls (ESPN Fantasy, Yahoo Fantasy, the NBA CDN).

Every request site in the provider services goes through `provider_get` (or
`provider_post` for Yahoo's OAuth token endpoint), so a failure is one typed
`AppError` from `core.errors` with a real HTTP status, the response body never
carries provider text, and the operator gets exactly one `provider_call_failed`
log line per failed call:

| what the provider did                          | raised                                   | HTTP |
|------------------------------------------------|------------------------------------------|------|
| 401 / 403 (cookies or token rejected)          | ProviderAuthError(provider)              | 403  |
| 404 on a GET (no such league / team)           | BadRequestError("LEAGUE_NOT_FOUND")      | 400  |
| other 4xx, 429, 5xx (5xx after one retry)      | ProviderError(provider)                  | 502  |
| connection error / timeout (after one retry)   | ProviderTimeout(provider)                | 504  |
| body not JSON, or `expect_key` missing from it | ProviderError("PROVIDER_BAD_RESPONSE")   | 502  |

Retries (GET only): one more attempt after 0.5 s on a network error or a 5xx,
through `core.resilience.with_retry`. 4xx, 429 and POSTs never retry.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from core.errors import AppError, BadRequestError, ProviderAuthError, ProviderError, ProviderTimeout
from core.logging import get_logger
from core.resilience import (
    ClientError,
    NetworkError,
    RateLimitError,
    ServerError,
    resilient_request,
    with_retry,
)
from core.settings import settings

BAD_RESPONSE_CODE = "PROVIDER_BAD_RESPONSE"
LEAGUE_NOT_FOUND_CODE = "LEAGUE_NOT_FOUND"

RETRY_MAX_ATTEMPTS = 2
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 1

DEFAULT_AUTH_STATUSES = (401, 403)

_LABELS = {"espn": "ESPN", "yahoo": "Yahoo", "nba": "NBA"}
_BODY_PREVIEW = 200


def provider_label(provider: str) -> str:
    """Display name for a provider key ("espn" -> "ESPN")."""
    return _LABELS.get(provider, provider.upper())


def _redacted_url(url: str) -> str:
    """Scheme, host and path only: never a query string in the logs."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _rate_limited(provider: str, exc: RateLimitError) -> ProviderError:
    return ProviderError(
        provider,
        f"{provider_label(provider)} is rate limiting requests — retry in a minute",
        data={"retry_after": exc.retry_after},
    )


@with_retry(max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY, max_delay=RETRY_MAX_DELAY)
def _get_once(provider: str, url: str, **kwargs: Any) -> requests.Response:
    """One GET; `with_retry` re-runs it once on NetworkError / ServerError only."""
    try:
        return resilient_request("GET", url, **kwargs)
    except RateLimitError as exc:
        # An AppError is not a RetryableError, so tenacity lets it straight through
        raise _rate_limited(provider, exc) from exc


def _post_once(provider: str, url: str, **kwargs: Any) -> requests.Response:
    """One POST, never retried (token exchanges are not idempotent)."""
    try:
        return resilient_request("POST", url, **kwargs)
    except RateLimitError as exc:
        raise _rate_limited(provider, exc) from exc


def _call(
    method: str,
    provider: str,
    url: str,
    *,
    expect_key: Optional[str],
    timeout: Optional[float],
    auth_statuses: tuple[int, ...],
    league_404: bool,
    **request_kwargs: Any,
) -> dict:
    log = get_logger("provider")
    label = provider_label(provider)
    started = time.perf_counter()
    status: Optional[int] = None
    error: Optional[AppError] = None
    body: Any = None
    preview: Optional[str] = None

    try:
        send = _get_once if method == "GET" else _post_once
        response = send(provider, url, timeout=timeout or settings.http_timeout, **request_kwargs)
    except ProviderError as exc:  # 429, converted inside the attempt so it is never retried
        status, error = 429, exc
    except ClientError as exc:
        status = exc.status_code
        if status in auth_statuses:
            error = ProviderAuthError(provider)
        elif status == 404 and league_404:
            error = BadRequestError(
                LEAGUE_NOT_FOUND_CODE,
                f"{label} league not found — check the league id and season",
            )
        else:
            error = ProviderError(provider, f"{label} rejected the request ({status}) — retry in a minute")
    except ServerError as exc:
        status = exc.status_code
        error = ProviderError(provider, f"{label} isn't responding — retry in a minute")
    except NetworkError:
        error = ProviderTimeout(provider, f"{label} timed out — retry in a minute")
    else:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            preview = (response.text or "")[:_BODY_PREVIEW]
            error = ProviderError(provider, f"{label} returned an unreadable response — retry in a minute",
                                  error_code=BAD_RESPONSE_CODE)
        else:
            if expect_key is not None and not (isinstance(body, dict) and expect_key in body):
                preview = (response.text or "")[:_BODY_PREVIEW]
                error = ProviderError(provider, f"{label} returned an unexpected response — retry in a minute",
                                      error_code=BAD_RESPONSE_CODE)

    if error is None:
        return body

    log.warning(
        "provider_call_failed",
        provider=provider,
        method=method,
        status=status,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        error_code=error.error_code,
        url=_redacted_url(url),
        body_preview=preview,
    )
    raise error


def provider_get(
    provider: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    expect_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict:
    """GET `url` and return its JSON body, or raise the typed `AppError` for the failure.

    `expect_key` names the top-level key a well-formed body must carry ("teams",
    "players", "fantasy_content"); a body without it is a `PROVIDER_BAD_RESPONSE`.
    """
    return _call(
        "GET", provider, url,
        expect_key=expect_key, timeout=timeout,
        auth_statuses=DEFAULT_AUTH_STATUSES, league_404=True,
        params=params, headers=headers, cookies=cookies,
    )


def provider_post(
    provider: str,
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    expect_key: Optional[str] = None,
    timeout: Optional[float] = None,
    auth_statuses: tuple[int, ...] = DEFAULT_AUTH_STATUSES,
) -> dict:
    """POST form data (no retry) and return the JSON body, or raise the typed `AppError`.

    Yahoo's token endpoint answers 400 when the grant (auth code / refresh token)
    is rejected, so that caller passes `auth_statuses=(400, 401, 403)`; a 404 is a
    plain `ProviderError` here (there is no league to be missing).
    """
    return _call(
        "POST", provider, url,
        expect_key=expect_key, timeout=timeout,
        auth_statuses=auth_statuses, league_404=False,
        data=data, headers=headers,
    )
