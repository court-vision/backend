"""Bounded asynchronous HTTP boundary for ESPN, Yahoo, and NBA providers."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
import sentry_sdk

from core.errors import AppError, BadRequestError, ProviderAuthError, ProviderError, ProviderTimeout
from core.logging import get_logger
from core.resilience import ClientError, NetworkError, RateLimitError, ServerError
from core.settings import settings

BAD_RESPONSE_CODE = "PROVIDER_BAD_RESPONSE"
LEAGUE_NOT_FOUND_CODE = "LEAGUE_NOT_FOUND"
RETRY_MAX_ATTEMPTS = 2
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 1
DEFAULT_AUTH_STATUSES = (401, 403)

_LABELS = {"espn": "ESPN", "yahoo": "Yahoo", "nba": "NBA"}
_BODY_PREVIEW = 200


class _NoCookiePersistenceTransport(httpx.AsyncBaseTransport):
    """Keep response cookies out of a process-wide client's cookie jar."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        if "set-cookie" in response.headers:
            del response.headers["set-cookie"]
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


_clients: dict[str, httpx.AsyncClient] = {}
_capacity: dict[str, asyncio.Semaphore] = {}


def _limit_for(provider: str) -> int:
    return {
        "espn": settings.espn_max_in_flight,
        "yahoo": settings.yahoo_max_in_flight,
        "nba": settings.nba_max_in_flight,
    }.get(provider, settings.nba_max_in_flight)


def _make_client(provider: str) -> httpx.AsyncClient:
    limit = _limit_for(provider)
    transport = _NoCookiePersistenceTransport(
        httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit))
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(settings.http_timeout, connect=5.0, write=10.0, pool=2.0),
        follow_redirects=True,
    )


def start_provider_runtime() -> None:
    if _clients:
        return
    for provider in _LABELS:
        _clients[provider] = _make_client(provider)
        _capacity[provider] = asyncio.Semaphore(_limit_for(provider))
    get_logger("provider").info(
        "provider_runtime_started",
        espn_max_in_flight=settings.espn_max_in_flight,
        yahoo_max_in_flight=settings.yahoo_max_in_flight,
        nba_max_in_flight=settings.nba_max_in_flight,
        queue_timeout_s=settings.provider_queue_timeout_seconds,
    )


async def stop_provider_runtime() -> None:
    clients = list(_clients.values())
    _clients.clear()
    _capacity.clear()
    await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


def _ensure_provider_runtime(provider: str) -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    if not _clients:
        start_provider_runtime()
    if provider not in _clients:
        _clients[provider] = _make_client(provider)
        _capacity[provider] = asyncio.Semaphore(_limit_for(provider))
    return _clients[provider], _capacity[provider]


def provider_label(provider: str) -> str:
    return _LABELS.get(provider, provider.upper())


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _rate_limited(provider: str, exc: RateLimitError) -> ProviderError:
    return ProviderError(
        provider,
        f"{provider_label(provider)} is rate limiting requests — retry in a minute",
        data={"retry_after": exc.retry_after},
    )


def _classify_response(response: httpx.Response) -> None:
    status = response.status_code
    if status == 429:
        value = response.headers.get("Retry-After")
        try:
            retry_after = int(value) if value else 60
        except ValueError:
            retry_after = 60
        raise RateLimitError("Rate limited", retry_after=retry_after)
    if status >= 500:
        raise ServerError("Server error", status_code=status)
    if status >= 400:
        raise ClientError("Client error", status_code=status)


def _cookie_header(cookies: Optional[dict]) -> Optional[str]:
    if not cookies:
        return None
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


async def _attempt(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout: Optional[float],
    **kwargs: Any,
) -> httpx.Response:
    try:
        request_kwargs = dict(kwargs)
        if timeout is not None:
            # Preserve the configured connect/write/pool ceilings even when a
            # caller supplies a shorter read timeout.
            request_kwargs["timeout"] = httpx.Timeout(
                timeout,
                connect=min(5.0, timeout),
                write=min(10.0, timeout),
                pool=min(2.0, timeout),
            )
        response = await client.request(method, url, **request_kwargs)
        _classify_response(response)
        return response
    except httpx.TimeoutException as exc:
        raise NetworkError("Request timed out") from exc
    except httpx.RequestError as exc:
        raise NetworkError("Request failed") from exc


async def _send(
    method: str,
    provider: str,
    url: str,
    *,
    timeout: Optional[float],
    **kwargs: Any,
) -> httpx.Response:
    client, capacity = _ensure_provider_runtime(provider)
    queued_at = time.perf_counter()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.provider_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        get_logger("provider").warning(
            "provider_capacity_timeout",
            provider=provider,
            queue_timeout_s=settings.provider_queue_timeout_seconds,
            max_in_flight=_limit_for(provider),
        )
        raise NetworkError("Provider capacity timeout") from exc

    queue_ms = round((time.perf_counter() - queued_at) * 1000)
    started = time.perf_counter()
    with sentry_sdk.start_span(op="http.client", name=f"{method} {provider}") as span:
        span.set_data("provider", provider)
        span.set_data("queue_ms", queue_ms)
        try:
            attempts = RETRY_MAX_ATTEMPTS if method == "GET" else 1
            for attempt in range(1, attempts + 1):
                try:
                    return await _attempt(client, method, url, timeout=timeout, **kwargs)
                except RateLimitError as exc:
                    raise _rate_limited(provider, exc) from exc
                except (NetworkError, ServerError):
                    if attempt >= attempts:
                        raise
                    get_logger("retry").warning(
                        "retry_attempt", function="provider_get", provider=provider, attempt=attempt
                    )
                    await asyncio.sleep(RETRY_BASE_DELAY)
            raise AssertionError("unreachable")
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            span.set_data("execution_ms", elapsed_ms)
            capacity.release()
            if queue_ms >= 50:
                get_logger("provider").warning(
                    "provider_queue_slow", provider=provider, queue_ms=queue_ms
                )


async def _call(
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
        response = await _send(method, provider, url, timeout=timeout, **request_kwargs)
    except ProviderError as exc:
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
            error = ProviderError(
                provider,
                f"{label} returned an unreadable response — retry in a minute",
                error_code=BAD_RESPONSE_CODE,
            )
        else:
            if expect_key is not None and not (isinstance(body, dict) and expect_key in body):
                preview = (response.text or "")[:_BODY_PREVIEW]
                error = ProviderError(
                    provider,
                    f"{label} returned an unexpected response — retry in a minute",
                    error_code=BAD_RESPONSE_CODE,
                )

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


async def provider_get(
    provider: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    expect_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict:
    request_headers = dict(headers or {})
    cookie = _cookie_header(cookies)
    if cookie:
        request_headers["Cookie"] = cookie
    return await _call(
        "GET", provider, url,
        expect_key=expect_key, timeout=timeout,
        auth_statuses=DEFAULT_AUTH_STATUSES, league_404=True,
        params=params, headers=request_headers,
    )


async def provider_post(
    provider: str,
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    expect_key: Optional[str] = None,
    timeout: Optional[float] = None,
    auth_statuses: tuple[int, ...] = DEFAULT_AUTH_STATUSES,
) -> dict:
    return await _call(
        "POST", provider, url,
        expect_key=expect_key, timeout=timeout,
        auth_statuses=auth_statuses, league_404=False,
        data=data, headers=dict(headers or {}),
    )
