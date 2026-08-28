"""Bounded worker boundary for provider SDKs that cannot run asynchronously."""

from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

import sentry_sdk

from core.errors import ProviderTimeout
from core.logging import get_logger
from core.settings import settings

T = TypeVar("T")

_executors: dict[str, ThreadPoolExecutor] = {}
_capacity: dict[str, asyncio.Semaphore] = {}


def _limit(provider: str) -> int:
    return {
        "nba": settings.nba_max_in_flight,
        "email": settings.email_max_in_flight,
    }.get(provider, settings.nba_max_in_flight)


def start_blocking_provider_runtime() -> None:
    # Email delivery remains synchronous, but has its own executor and its own
    # limit so it can never consume the workers reserved for live NBA traffic.
    for provider in ("nba", "email"):
        _ensure_provider(provider)


def _ensure_provider(provider: str) -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
    if provider not in _executors:
        limit = _limit(provider)
        _executors[provider] = ThreadPoolExecutor(
            max_workers=limit,
            thread_name_prefix=f"courtvision-{provider}",
        )
        _capacity[provider] = asyncio.Semaphore(limit)
    return _executors[provider], _capacity[provider]


async def stop_blocking_provider_runtime() -> None:
    executors = list(_executors.values())
    _executors.clear()
    _capacity.clear()
    await asyncio.gather(*(
        asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        for executor in executors
    ))


async def run_blocking_provider(provider: str, operation: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    executor, capacity = _ensure_provider(provider)
    queued_at = time.perf_counter()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.provider_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        get_logger("provider").warning(
            "provider_capacity_timeout",
            provider=provider,
            operation=operation,
            max_in_flight=_limit(provider),
        )
        raise ProviderTimeout(provider) from exc

    queue_ms = round((time.perf_counter() - queued_at) * 1000)
    context = contextvars.copy_context()

    def _call() -> T:
        started = time.perf_counter()
        with sentry_sdk.start_span(op="provider.blocking", name=operation) as span:
            span.set_data("provider", provider)
            span.set_data("queue_ms", queue_ms)
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                span.set_data("execution_ms", elapsed_ms)
                if queue_ms >= 50 or elapsed_ms >= 250:
                    get_logger("provider").warning(
                        "blocking_provider_operation_slow",
                        provider=provider,
                        operation=operation,
                        queue_ms=queue_ms,
                        elapsed_ms=elapsed_ms,
                    )

    try:
        future = asyncio.get_running_loop().run_in_executor(executor, context.run, _call)
    except Exception:
        capacity.release()
        raise
    future.add_done_callback(lambda _: capacity.release())
    return await asyncio.shield(future)
