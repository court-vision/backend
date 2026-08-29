"""Bounded worker boundary for CPU-bound request work.

Category z-scoring and response assembly are pure Python: no database, no
network. Two places they must not run:

- inside `run_db`, where they hold one of the `db_max_in_flight` permits for
  their whole duration and starve every other endpoint of database capacity
  (this is why the category rankings path degraded 3.4x faster than the points
  path under concurrency);
- on the event loop, where they block every other request for the same span.

So they get their own small pool. It is deliberately smaller than the DB pool:
this work is GIL-bound, so extra workers buy latency, not throughput, and the
limit is what stops a burst of expensive renders from monopolising the
interpreter. Work queued behind a full pool for longer than
`cpu_queue_timeout_seconds` is shed as a 503 rather than piling up.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

import sentry_sdk

from core.errors import ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings

T = TypeVar("T")

log = get_logger("compute")

_executor: ThreadPoolExecutor | None = None
_capacity: asyncio.Semaphore | None = None


def start_cpu_runtime() -> None:
    """Create the CPU executor on application startup."""
    global _executor, _capacity
    if _executor is not None:
        return
    _executor = ThreadPoolExecutor(
        max_workers=settings.cpu_max_in_flight,
        thread_name_prefix="courtvision-cpu",
    )
    _capacity = asyncio.Semaphore(settings.cpu_max_in_flight)
    log.info(
        "cpu_runtime_started",
        max_in_flight=settings.cpu_max_in_flight,
        queue_timeout_s=settings.cpu_queue_timeout_seconds,
    )


async def stop_cpu_runtime() -> None:
    """Stop accepting CPU work and close the executor."""
    global _executor, _capacity
    executor, _executor, _capacity = _executor, None, None
    if executor is not None:
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


def _ensure_cpu_runtime() -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
    # Service tests and scripts do not run FastAPI's lifespan; lazily creating
    # the same resources keeps those entry points usable (as run_db does).
    if _executor is None or _capacity is None:
        start_cpu_runtime()
    assert _executor is not None and _capacity is not None
    return _executor, _capacity


async def run_cpu(operation_name: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run one CPU-bound function in the bounded compute executor.

    Capacity stays charged until the worker really finishes, even when the
    awaiting request is cancelled, so an abandoned render cannot push the
    process past the configured CPU concurrency budget.
    """
    executor, capacity = _ensure_cpu_runtime()
    queued_at = time.perf_counter()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.cpu_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        log.error(
            "cpu_capacity_timeout",
            operation=operation_name,
            timeout_s=settings.cpu_queue_timeout_seconds,
            max_in_flight=settings.cpu_max_in_flight,
        )
        raise ServiceUnavailableError(message="Server is busy — retry in a moment") from exc

    queue_ms = round((time.perf_counter() - queued_at) * 1000)
    context = contextvars.copy_context()

    def _call() -> T:
        started = time.perf_counter()
        with sentry_sdk.start_span(op="cpu.task", name=operation_name) as span:
            span.set_data("cpu.queue_ms", queue_ms)
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                span.set_data("cpu.execution_ms", elapsed_ms)
                if queue_ms >= 50 or elapsed_ms >= 250:
                    log.warning(
                        "cpu_operation_slow",
                        operation=operation_name,
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
