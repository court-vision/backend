import asyncio
import contextvars
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Callable, TypeVar

import sentry_sdk
from peewee import InterfaceError, Model, OperationalError
from playhouse.db_url import parse
from playhouse.pool import PooledPostgresqlExtDatabase

from core.logging import get_logger
from core.errors import DatabaseUnavailableError
from core.settings import settings

# Get database credentials from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
parsed_url = parse(DATABASE_URL)
db_name = parsed_url.pop('database')

db = PooledPostgresqlExtDatabase(
    db_name,
    max_connections=settings.db_pool_max_connections,
    stale_timeout=300,
    # psycopg2 connect_timeout: a dead/unreachable host fails fast (503) instead of hanging a worker
    connect_timeout=10,
    # TCP keepalives: a half-open socket (Postgres restart, private-network blip) is detected in
    # ~60 s instead of blocking a query — and therefore the event loop — for the kernel's
    # retransmit budget (15+ min). statement_timeout bounds any single query the same way.
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=30000",
    **parsed_url
)

log = get_logger("db")

T = TypeVar("T")
DB_RUNTIME_ERRORS = (DatabaseUnavailableError, OperationalError, InterfaceError)

_executor: ThreadPoolExecutor | None = None
_capacity: asyncio.Semaphore | None = None


def start_db_runtime() -> None:
    """Create the request DB executor on application startup."""
    global _executor, _capacity
    if _executor is not None:
        return
    _executor = ThreadPoolExecutor(max_workers=settings.db_max_in_flight, thread_name_prefix="courtvision-db")
    _capacity = asyncio.Semaphore(settings.db_max_in_flight)
    log.info(
        "db_runtime_started",
        max_in_flight=settings.db_max_in_flight,
        pool_max_connections=settings.db_pool_max_connections,
        queue_timeout_s=settings.db_queue_timeout_seconds,
    )


async def stop_db_runtime() -> None:
    """Stop accepting DB work and close the dedicated executor."""
    global _executor, _capacity
    executor, _executor, _capacity = _executor, None, None
    if executor is not None:
        # Lifespan shutdown runs after active requests have drained. Do not block
        # the event loop while the executor joins its worker threads.
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
    try:
        db.close_all()
    except Exception as exc:
        log.warning("db_pool_close_failed", error=type(exc).__name__)


def _ensure_db_runtime() -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
    # Direct service tests and scripts do not run FastAPI's lifespan. Lazily
    # creating the same resources keeps those entry points usable.
    if _executor is None or _capacity is None:
        start_db_runtime()
    assert _executor is not None and _capacity is not None
    return _executor, _capacity


class BaseModel(Model):
    class Meta:
        database = db


def init_db():
    """Apply pending schema migrations, then open the connection pool.

    Schema is owned by backend/migrations (see migrations/README.md); Peewee
    models describe tables but never create them.
    """
    from .migrate import apply_migrations

    apply_migrations(DATABASE_URL)
    # A pooled database has no separate "open pool" operation. Do not leave an
    # unused connection attached to the event-loop thread after startup.
    if not db.is_closed():
        db.close()


# Function to close database connection
def close_db():
    """Close database connection."""
    if not db.is_closed():
        db.close()
        log.info("database_connection_closed")


async def run_db(operation_name: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run one materialized Peewee operation in the bounded DB executor.

    Capacity remains charged until the worker really finishes, even when the
    awaiting request is cancelled. This prevents abandoned psycopg2 calls from
    silently exceeding the configured database concurrency budget.
    """
    executor, capacity = _ensure_db_runtime()
    queued_at = time.perf_counter()
    try:
        await asyncio.wait_for(capacity.acquire(), settings.db_queue_timeout_seconds)
    except asyncio.TimeoutError as exc:
        log.error(
            "db_capacity_timeout",
            operation=operation_name,
            timeout_s=settings.db_queue_timeout_seconds,
            max_in_flight=settings.db_max_in_flight,
        )
        raise DatabaseUnavailableError() from exc

    queue_ms = round((time.perf_counter() - queued_at) * 1000)
    context = contextvars.copy_context()

    def _call() -> T:
        started = time.perf_counter()
        with sentry_sdk.start_span(op="db.query", name=operation_name) as span:
            span.set_data("db.queue_ms", queue_ms)
            try:
                with db.connection_context():
                    return fn(*args, **kwargs)
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                span.set_data("db.execution_ms", elapsed_ms)
                if queue_ms >= 50 or elapsed_ms >= 250:
                    log.warning(
                        "db_operation_slow",
                        operation=operation_name,
                        queue_ms=queue_ms,
                        elapsed_ms=elapsed_ms,
                    )

    try:
        future = asyncio.get_running_loop().run_in_executor(executor, context.run, _call)
    except Exception:
        capacity.release()
        raise

    # Done callbacks for asyncio futures execute on the event-loop thread. The
    # permit is therefore released safely after the actual worker exits.
    future.add_done_callback(lambda _: capacity.release())
    return await asyncio.shield(future)


def db_operation(operation_name: str) -> Callable[[Callable[..., T]], Callable[..., Any]]:
    """Turn a synchronous, fully-materialized repository function into an async boundary."""
    def decorator(fn: Callable[..., T]) -> Callable[..., Any]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await run_db(operation_name, fn, *args, **kwargs)
        return wrapper
    return decorator
