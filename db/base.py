import asyncio
import contextvars
import threading
import time
import weakref
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
from core.tracing import timed, waiting

# One source of truth for the connection string. `Settings` reads the process
# environment first (how Railway supplies it) and falls back to backend/.env
# (how local dev does). Reading os.getenv here instead meant `.env` was silently
# ignored: the documented local run path crashed on `parse(None)`, and the only
# way to boot was to export a URL by hand — which is how a laptop ends up
# talking to production.
DATABASE_URL = settings.database_url
parsed_url = parse(DATABASE_URL)
db_name = parsed_url.pop('database')

db = PooledPostgresqlExtDatabase(
    db_name,
    max_connections=settings.db_pool_max_connections,
    # Age, not idle time: see db_pool_stale_timeout_seconds in core/settings.py
    # for why this is no longer 300 s.
    stale_timeout=settings.db_pool_stale_timeout_seconds,
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


# How long each warm-up worker waits for the others to open theirs. Connects
# are serial (Peewee holds a process-wide lock around psycopg2.connect), so 8
# take about 8 x 14-28 ms in prod; this only bounds a database that answers
# migrations and then stalls.
WARM_POOL_WAIT_S = 5.0


async def warm_db_pool(count: int) -> None:
    """Open `count` pooled connections before the first request (application startup).

    The pool starts empty after a deploy, and Peewee opens connections one at a
    time, so the first page open's parallel requests queued behind each other's
    connects. Opening them here moves that wait before traffic.

    Each connection is opened on its own DB worker, and every worker holds its
    connection until all of them have one: a worker that released early would
    hand its connection straight to the next, and the pool would end up with
    one connection, not `count`. Best effort: a failure is logged and never
    fails startup; requests open whatever is missing, as they always have.
    """
    count = min(count, settings.db_max_in_flight)
    if count <= 0:
        return
    executor, capacity = _ensure_db_runtime()
    barrier = threading.Barrier(count)

    def _open_and_hold() -> None:
        try:
            db.connect(reuse_if_open=True)
        except Exception:
            barrier.abort()  # the others stop waiting for a connection that will not come
            raise
        try:
            barrier.wait(timeout=WARM_POOL_WAIT_S)
        finally:
            _mark_released()
            db.close()  # back into the pool

    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    futures = []
    for _ in range(count):
        # Free at startup; charged like any run_db so the worker budget stays honest.
        await capacity.acquire()
        try:
            future = loop.run_in_executor(executor, _open_and_hold)
        except Exception:
            capacity.release()
            barrier.abort()
            break
        future.add_done_callback(lambda _: capacity.release())
        futures.append(future)

    try:
        results = await asyncio.wait_for(
            asyncio.shield(asyncio.gather(*futures, return_exceptions=True)),
            WARM_POOL_WAIT_S * 2,
        )
    except asyncio.TimeoutError:
        log.warning("db_pool_warm_timeout", connections=count, timeout_s=WARM_POOL_WAIT_S * 2)
        return

    connect_ms = round((time.perf_counter() - started) * 1000)
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures or len(results) < count:
        # The workers that only saw the barrier break are not the story.
        cause = next((f for f in failures if not isinstance(f, threading.BrokenBarrierError)),
                     failures[0] if failures else None)
        log.warning(
            "db_pool_warm_failed",
            connections=count,
            failed=len(failures) + count - len(results),
            error=type(cause).__name__ if cause else None,
            connect_ms=connect_ms,
        )
        return
    # Connects are serial, so connect_ms is their sum: the wait a cold burst
    # of `count` parallel requests would otherwise have queued behind.
    log.info("db_pool_warmed", connections=count, connect_ms=connect_ms,
             per_connection_ms=round(connect_ms / count))


# When run_db last handed each pooled connection back, to tell how long it has
# sat idle (Peewee's pool keeps only its creation time). Weak keys: an entry
# goes with the connection once the pool has dropped it.
_released_at: "weakref.WeakKeyDictionary[Any, float]" = weakref.WeakKeyDictionary()


def _mark_released() -> None:
    if not db.is_closed():
        _released_at[db.connection()] = time.monotonic()


def _use_live_connection(operation_name: str) -> None:
    """Swap this thread's pooled connection for a live one if it died while idle.

    Peewee's pool checks only libpq's local state at checkout, so a connection
    whose server went away (a Postgres restart or failover) looks healthy until
    a query on it fails, and its request with it (a 503). Pooled connections
    live an hour (see db_pool_stale_timeout_seconds), so after a restart each
    idle one would fail a request that way. A connection idle for
    db_pool_ping_after_idle_seconds or more, or one run_db has not handed out
    before, is pinged first, and a dead one is dropped and another checked out.
    A connection used moments ago is trusted, which keeps the ping off the
    queries of a page open's burst. The ping runs after checkout, outside
    Peewee's locks, so a slow one holds up only this operation.
    """
    if db.is_closed():  # no pooled connection to check (unit tests replace connection_context)
        return
    # Each dead connection found is discarded, so this ends within one pool's worth.
    for _ in range(settings.db_pool_max_connections + 1):
        conn = db.connection()
        released = _released_at.pop(conn, None)
        idle_s = None if released is None else time.monotonic() - released
        if idle_s is not None and idle_s < settings.db_pool_ping_after_idle_seconds:
            return
        try:
            db.execute_sql("SELECT 1").close()
            return
        except (OperationalError, InterfaceError) as exc:
            log.warning(
                "db_connection_dead",
                operation=operation_name,
                idle_s=None if idle_s is None else round(idle_s),
                error=type(exc).__name__,
            )
            # Not db.manual_close(): it takes the pool lock before the database
            # lock, the reverse of connect() and close(), and can deadlock with
            # another worker's checkout. A closed connection handed back is
            # dropped by the pool (libpq reports its transaction status unknown).
            conn.close()
            db.close()
            db.connect()


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
        with waiting("db slot"):
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
        # The whole operation is Postgres time (checkout, queries, reading the
        # rows); its queries are the calls (core/tracing.py traces each one).
        with sentry_sdk.start_span(op="db.query", name=operation_name) as span, \
                timed("postgres", operation_name, call=False):
            span.set_data("db.queue_ms", queue_ms)
            try:
                with db.connection_context():
                    _use_live_connection(operation_name)
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        _mark_released()
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
