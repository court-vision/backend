"""Bounded HTTP client for the private SQLMate service.

Only the explicit API proxy routes call this module. SQLMate has no public
domain; browsers reach it through Court Vision's API, which supplies public
rate limiting and authenticated boundaries for user-table operations.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from core.errors import ServiceUnavailableError
from core.logging import get_correlation_id, get_logger
from core.settings import settings

UNAVAILABLE_MESSAGE = "Query builder service unavailable — try again in a minute"

_client: Optional[httpx.AsyncClient] = None
_capacity: Optional[asyncio.Semaphore] = None


def make_client() -> httpx.AsyncClient:
    limit = settings.sqlmate_max_in_flight
    return httpx.AsyncClient(
        base_url=settings.sqlmate_internal_url.rstrip("/"),
        timeout=httpx.Timeout(
            settings.sqlmate_timeout_seconds,
            connect=min(5.0, settings.sqlmate_timeout_seconds),
            write=min(10.0, settings.sqlmate_timeout_seconds),
            pool=min(2.0, settings.sqlmate_timeout_seconds),
        ),
        limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
    )


def start_sqlmate_runtime() -> None:
    global _client, _capacity
    if _client is not None:
        return
    _client = make_client()
    _capacity = asyncio.Semaphore(settings.sqlmate_max_in_flight)
    get_logger("sqlmate").info(
        "sqlmate_runtime_started",
        max_in_flight=settings.sqlmate_max_in_flight,
        queue_timeout_s=settings.provider_queue_timeout_seconds,
    )


async def stop_sqlmate_runtime() -> None:
    global _client, _capacity
    client, _client, _capacity = _client, None, None
    if client is not None:
        await client.aclose()


def _ensure_runtime() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    if _client is None or _capacity is None:
        start_sqlmate_runtime()
    assert _client is not None and _capacity is not None
    return _client, _capacity


async def proxy_request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: Optional[dict[str, str]] = None,
    authorization: Optional[str] = None,
) -> httpx.Response:
    """Send one allow-listed proxy request without retrying mutations."""
    client, capacity = _ensure_runtime()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.provider_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        get_logger("sqlmate").warning(
            "sqlmate_capacity_timeout",
            max_in_flight=settings.sqlmate_max_in_flight,
            queue_timeout_s=settings.provider_queue_timeout_seconds,
        )
        raise ServiceUnavailableError("SQLMATE_UNAVAILABLE", UNAVAILABLE_MESSAGE) from exc

    headers = {"Accept": "application/json"}
    correlation_id = get_correlation_id()
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    if authorization:
        headers["Authorization"] = authorization

    started = time.perf_counter()
    try:
        response = await client.request(method, path, json=json, params=params, headers=headers)
    except httpx.RequestError as exc:
        get_logger("sqlmate").error(
            "sqlmate_request_failed",
            method=method,
            path=path,
            error=type(exc).__name__,
            detail=str(exc),
        )
        raise ServiceUnavailableError("SQLMATE_UNAVAILABLE", UNAVAILABLE_MESSAGE) from exc
    finally:
        capacity.release()

    get_logger("sqlmate").info(
        "sqlmate_request",
        method=method,
        path=path,
        status_code=response.status_code,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )
    return response
