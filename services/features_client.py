"""
HTTP client for the lineup-generation ("features") service.

The one place every caller of `POST /generate-lineup` shares: the timeout, the
retry policy for a service that may be waking from sleep, the mapping of the
service's responses to typed errors, and the structured log line per attempt.

Callers (`LineupService`, `OptimizeService`) use `request_lineup(payload)` and
turn `FeaturesRejected` / `FeaturesUnavailable` into ERROR responses; tests swap
`client_factory` for an `httpx.AsyncClient(transport=httpx.MockTransport(...))`.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import httpx

from core.logging import get_correlation_id, get_logger
from core.settings import settings
from utils.constants import FEATURES_SERVER_ENDPOINT

# The optimizer runs a genetic algorithm over the whole week: generous read timeout,
# short connect timeout so a sleeping/unreachable service fails fast and is retried.
FEATURES_TIMEOUT = httpx.Timeout(90.0, connect=15.0)

# Errors worth one more attempt: the Railway service waking up, a dropped connection
# or a truncated response. Non-retryable failures (4xx/5xx bodies) never loop.
RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0

# Where the service lives. Module-level so a rehearsal can point at a local instance.
FEATURES_URL = FEATURES_SERVER_ENDPOINT

UNAVAILABLE_MESSAGE = "Lineup service unavailable, try again in a minute"


class FeaturesError(Exception):
    """Base class: the features service could not produce a lineup."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FeaturesRejected(FeaturesError):
    """The service rejected the request itself (400/422), e.g. a week outside its calendar."""


class FeaturesUnavailable(FeaturesError):
    """The service could not be reached, timed out, or failed (5xx / unreadable body)."""


def make_client() -> httpx.AsyncClient:
    """Create the process-wide client; per-request headers are added at call time."""
    limit = settings.features_max_in_flight
    return httpx.AsyncClient(
        base_url=FEATURES_URL,
        timeout=FEATURES_TIMEOUT,
        limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
    )


# Tests may monkeypatch this with a MockTransport-backed factory. Production
# leaves it as None and reuses the lifespan-owned client.
client_factory: Optional[Callable[[], httpx.AsyncClient]] = None
_client: Optional[httpx.AsyncClient] = None
_capacity: Optional[asyncio.Semaphore] = None


def start_features_runtime() -> None:
    global _client, _capacity
    if _client is not None:
        return
    _client = make_client()
    _capacity = asyncio.Semaphore(settings.features_max_in_flight)
    get_logger("features").info(
        "features_runtime_started",
        max_in_flight=settings.features_max_in_flight,
        queue_timeout_s=settings.provider_queue_timeout_seconds,
    )


async def stop_features_runtime() -> None:
    global _client, _capacity
    client, _client, _capacity = _client, None, None
    if client is not None:
        await client.aclose()


@asynccontextmanager
async def _request_client():
    if client_factory is not None:
        async with client_factory() as client:
            yield client
        return
    if _client is None or _capacity is None:
        start_features_runtime()
    assert _client is not None
    yield _client


def _request_headers() -> dict[str, str]:
    correlation_id = get_correlation_id()
    return {"X-Correlation-ID": correlation_id} if correlation_id else {}


async def _acquire_capacity() -> asyncio.Semaphore:
    if _capacity is None:
        start_features_runtime()
    assert _capacity is not None
    try:
        await asyncio.wait_for(_capacity.acquire(), settings.provider_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        get_logger("features").warning(
            "features_capacity_timeout",
            max_in_flight=settings.features_max_in_flight,
            queue_timeout_s=settings.provider_queue_timeout_seconds,
        )
        raise FeaturesUnavailable(UNAVAILABLE_MESSAGE) from exc
    return _capacity


def _first_value_kind(payload: dict) -> Optional[str]:
    for player in list(payload.get("roster_data") or []) + list(payload.get("free_agent_data") or []):
        kind = player.get("value_kind") if isinstance(player, dict) else None
        if kind:
            return kind
    return None


def _log_fields(payload: dict, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "week": payload.get("week"),
        "roster": len(payload.get("roster_data") or []),
        "free_agents": len(payload.get("free_agent_data") or []),
        "streaming_slots": payload.get("streaming_slots"),
        "value_kind": _first_value_kind(payload),
        **context,
    }


async def post_generate_lineup(payload: dict, **context: Any) -> httpx.Response:
    """POST the payload to /generate-lineup, retrying once on a connection-level failure.

    Returns whatever the service answered (any status code); raises
    `FeaturesUnavailable` only when every attempt failed to get a response.
    `context` (e.g. team_id, caller) is added to the log line.
    """
    log = get_logger()
    fields = _log_fields(payload, context)
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.perf_counter()
        capacity = await _acquire_capacity()
        retry_error: Optional[Exception] = None
        try:
            async with _request_client() as client:
                response = await client.post("/generate-lineup", json=payload, headers=_request_headers())
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            retry_error = exc
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if attempt < MAX_ATTEMPTS:
                log.warning("features_generate_lineup_retry", attempt=attempt, elapsed_ms=elapsed_ms,
                            error=type(exc).__name__, detail=str(exc), **fields)
            else:
                log.error("features_generate_lineup_unavailable", attempt=attempt, elapsed_ms=elapsed_ms,
                          error=type(exc).__name__, detail=str(exc), **fields)
        finally:
            capacity.release()

        if retry_error is not None:
            if attempt >= MAX_ATTEMPTS:
                break
            # A backoff is not an in-flight service call, so it must not occupy
            # one of the four shared features permits.
            if RETRY_DELAY_SECONDS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            continue
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        log.info("features_generate_lineup", attempt=attempt, elapsed_ms=elapsed_ms,
                 status_code=response.status_code, **fields)
        return response

    raise FeaturesUnavailable(UNAVAILABLE_MESSAGE) from last_error


def _rejection_message(response: httpx.Response) -> str:
    """The service's own reason, e.g. 'unknown week 99 (calendar has 24 weeks)'."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        message = str(body["error"])
        if body.get("week") is not None and body.get("weeks") is not None:
            message = f"{message} {body['week']} (calendar has {body['weeks']} weeks)"
        return message
    text = (response.text or "").strip()
    return text or f"Lineup service rejected the request ({response.status_code})"


async def request_lineup(payload: dict, **context: Any) -> dict:
    """Generate a lineup: the parsed service response on success.

    Raises `FeaturesRejected` (with the service's message) for a 400/422 and
    `FeaturesUnavailable` for anything else that is not a readable 2xx.
    """
    response = await post_generate_lineup(payload, **context)
    status = response.status_code

    if status in (400, 422):
        message = _rejection_message(response)
        get_logger().warning("features_generate_lineup_rejected", status_code=status, detail=message,
                             **_log_fields(payload, context))
        raise FeaturesRejected(message, status)

    if not (200 <= status < 300):
        get_logger().error("features_generate_lineup_failed", status_code=status,
                           body=(response.text or "")[:300], **_log_fields(payload, context))
        raise FeaturesUnavailable(UNAVAILABLE_MESSAGE, status)

    try:
        return response.json()
    except ValueError as exc:
        get_logger().error("features_generate_lineup_unreadable", status_code=status,
                           body=(response.text or "")[:300], **_log_fields(payload, context))
        raise FeaturesUnavailable(UNAVAILABLE_MESSAGE, status) from exc


async def get_healthz() -> dict:
    """`GET /healthz` → {"status", "schedule_file", "weeks"} (raises on failure)."""
    capacity = await _acquire_capacity()
    try:
        async with _request_client() as client:
            response = await client.get("/healthz", headers=_request_headers())
            response.raise_for_status()
            return response.json()
    finally:
        capacity.release()
