"""Bounded HTTP client for the private fantasy-writer service.

fantasy-writer is the only place the ESPN transaction protocol lives; this API
is its only client, reached over Railway private networking with a bearer
token. A lineup write is a POST that ESPN executes immediately, so the client
**never retries** — an ambiguous timeout is reported as unavailable and the
caller re-reads the roster to learn what happened.

`client_factory` exists for tests (an `httpx.AsyncClient` over a MockTransport).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from core.logging import get_correlation_id, get_logger
from core.settings import settings

UNAVAILABLE_MESSAGE = "Lineup write service unavailable — try again in a minute"
LINEUP_PATH = "/v1/espn/lineup"

log = get_logger("fantasy_writer")


class FantasyWriterError(Exception):
    """Base: the writer answered, or failed to, without ESPN accepting the transaction."""

    def __init__(self, message: str, *, espn_status: Optional[int] = None, excerpt: Optional[str] = None,
                 espn_error_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.espn_status = espn_status
        self.excerpt = excerpt
        self.espn_error_code = espn_error_code


class FantasyWriterRejected(FantasyWriterError):
    """ESPN refused the transaction (locked player, ineligible slot, ...)."""


class FantasyWriterAuthRejected(FantasyWriterError):
    """ESPN answered 401/403: the stored cookies no longer authenticate."""


class FantasyWriterUnavailable(FantasyWriterError):
    """No usable answer: writer down, timeout, ESPN 5xx, bad token, or an unparseable body."""


@dataclass(frozen=True)
class WriterResult:
    ok: bool
    http_status: int                 # the writer's status
    espn_status: Optional[int]       # ESPN's status as relayed
    excerpt: Optional[str]
    idempotent_replay: bool = False


def make_client() -> httpx.AsyncClient:
    limit = settings.fantasy_writer_max_in_flight
    timeout = settings.fantasy_writer_timeout_seconds
    return httpx.AsyncClient(
        base_url=settings.fantasy_writer_url.rstrip("/"),
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout), write=min(10.0, timeout), pool=min(2.0, timeout)),
        limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
    )


client_factory: Optional[Callable[[], httpx.AsyncClient]] = None
_client: Optional[httpx.AsyncClient] = None
_capacity: Optional[asyncio.Semaphore] = None


def start_fantasy_writer_runtime() -> None:
    global _client, _capacity
    if _client is not None:
        return
    _client = make_client()
    _capacity = asyncio.Semaphore(settings.fantasy_writer_max_in_flight)
    log.info("fantasy_writer_runtime_started", max_in_flight=settings.fantasy_writer_max_in_flight,
             writes_enabled=settings.roster_writes_enabled)


async def stop_fantasy_writer_runtime() -> None:
    global _client, _capacity
    client, _client, _capacity = _client, None, None
    if client is not None:
        await client.aclose()


def _ensure_runtime() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    if _client is None or _capacity is None:
        start_fantasy_writer_runtime()
    assert _client is not None and _capacity is not None
    return _client, _capacity


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = settings.fantasy_writer_token.get_secret_value() if settings.fantasy_writer_token else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    correlation_id = get_correlation_id()
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return headers


def espn_error(body: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(message, code) from a writer answer. The writer relays what it parsed as
    `espn_message` / `espn_error_code`; failing that, ESPN's own error body is JSON
    shaped {"messages": [...], "details": [{"shortMessage", "type", ...}]} and is
    read from the excerpt, so the user sees prose, never the raw dict."""
    message, code = body.get("espn_message"), body.get("espn_error_code")
    if message:
        return message, code
    excerpt = body.get("espn_body_excerpt")
    if isinstance(excerpt, str) and excerpt.lstrip().startswith("{"):
        try:
            parsed = json.loads(excerpt)
        except ValueError:
            return None, code
        details = parsed.get("details") or []
        first = details[0] if details and isinstance(details[0], dict) else {}
        messages = parsed.get("messages") or []
        message = first.get("shortMessage") or first.get("message") or (messages[0] if messages else None)
        return (message or None), (code or first.get("type"))
    if isinstance(excerpt, str) and excerpt.strip() and len(excerpt) <= 200:
        return excerpt.strip(), code  # a short plain-text body is still ESPN's own words
    return None, code


def _parse(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def apply_lineup(payload: dict[str, Any]) -> WriterResult:
    """POST one lineup transaction. Exactly one attempt; the body never appears in logs."""
    client, capacity = _ensure_runtime()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.provider_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        log.warning("fantasy_writer_capacity_timeout", max_in_flight=settings.fantasy_writer_max_in_flight)
        raise FantasyWriterUnavailable(UNAVAILABLE_MESSAGE) from exc

    started = time.perf_counter()
    try:
        if client_factory is not None:
            async with client_factory() as test_client:
                response = await test_client.post(LINEUP_PATH, json=payload, headers=_headers())
        else:
            response = await client.post(LINEUP_PATH, json=payload, headers=_headers())
    except httpx.RequestError as exc:
        log.error("fantasy_writer_request_failed", error=type(exc).__name__, detail=str(exc),
                  move_count=len(payload.get("moves") or []))
        raise FantasyWriterUnavailable(UNAVAILABLE_MESSAGE) from exc
    finally:
        capacity.release()

    body = _parse(response)
    espn_status = body.get("http_status")
    excerpt = body.get("espn_body_excerpt")
    log.info(
        "fantasy_writer_request",
        status_code=response.status_code,
        espn_status=espn_status,
        error_code=body.get("error_code"),
        move_count=len(payload.get("moves") or []),
        idempotent_replay=bool(body.get("idempotent_replay")),
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )

    if response.status_code == 200 and body.get("ok"):
        return WriterResult(True, response.status_code, espn_status, excerpt, bool(body.get("idempotent_replay")))
    if response.status_code == 409:
        message, code = espn_error(body)
        raise FantasyWriterRejected(message or "ESPN rejected the lineup change", espn_status=espn_status,
                                    excerpt=excerpt, espn_error_code=code)
    if response.status_code == 403:
        raise FantasyWriterAuthRejected("ESPN no longer accepts the stored credentials", espn_status=espn_status, excerpt=excerpt)
    if response.status_code == 422:
        raise FantasyWriterRejected(body.get("detail") or "The writer refused the moves", espn_status=None, excerpt=excerpt)
    raise FantasyWriterUnavailable(UNAVAILABLE_MESSAGE, espn_status=espn_status, excerpt=excerpt)
