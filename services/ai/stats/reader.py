"""
The connection query_stats runs on.

With ASK_READER_DATABASE_URL set, a second small pool as the read-only
`ask_reader` role (ops/roles.sql): SELECT on the two nba.ask_* views only, a
2 s statement timeout, read-only transactions, all set at the role level and
repeated here as connection options. Without it (tests, local dev), the main
pool inside a transaction made read-only with the same timeout, which is
logged once at first use so a production process running that way is
noticed.

The server writes every statement it runs here (the compiler), so the role is
defence in depth: a compiler bug must still be unable to read usr.* or to
hold a connection for the main pool's 30 s.

Queries run on the shared DB executor through run_db, so they count against
the same in-flight budget and slow-query log as every other read. On the
dedicated path that holds one main-pool connection idle for the query's
duration (at most 2 s); the pool refuses rather than waits when it is full,
so the two pools cannot deadlock.
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Sequence

from peewee import InterfaceError, OperationalError
from playhouse.db_url import parse
from playhouse.pool import PooledPostgresqlExtDatabase

from core.errors import AppError, DatabaseUnavailableError
from core.logging import get_logger
from core.settings import settings
from db.base import db, run_db

log = get_logger("ai.stats")

STATEMENT_TIMEOUT_MS = 2000
MAX_CONNECTIONS = 4
OPERATION = "ai.query_stats"

# Postgres' SQLSTATE for "canceling statement due to statement timeout".
_QUERY_CANCELED = "57014"


class StatsQueryTimeout(AppError):
    status_code = 504
    error_code = "STATS_QUERY_TIMEOUT"
    default_message = (
        f"The query took longer than {STATEMENT_TIMEOUT_MS // 1000} s. "
        "Narrow it: fewer players or seasons, or a window."
    )
    log_level = "warning"


class StatsReader:
    """Runs a read-only statement and returns its rows as dicts."""

    def __init__(self, url: Optional[str]):
        self._db: PooledPostgresqlExtDatabase | None = None
        if url:
            parsed = parse(url)
            name = parsed.pop("database")
            self._db = PooledPostgresqlExtDatabase(
                name,
                max_connections=MAX_CONNECTIONS,
                stale_timeout=settings.db_pool_stale_timeout_seconds,
                connect_timeout=5,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
                options=(
                    f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
                    " -c default_transaction_read_only=on"
                    f" -c idle_in_transaction_session_timeout={STATEMENT_TIMEOUT_MS}"
                ),
                **parsed,
            )
        self._announced = False
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        """`ask_reader` on the dedicated pool, `main_pool` on the fallback."""
        return "ask_reader" if self._db is not None else "main_pool"

    @property
    def pool(self) -> PooledPostgresqlExtDatabase | None:
        return self._db

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run one SELECT. Raises StatsQueryTimeout past the 2 s limit."""
        return await run_db(OPERATION, self._run, sql, tuple(params))

    def _run(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        self._announce()
        try:
            if self._db is not None:
                return self._run_dedicated(sql, params)
            return self._run_fallback(sql, params)
        except Exception as exc:
            # Peewee wraps most driver errors in its own; a cancelled statement
            # (psycopg2.errors.QueryCanceled) comes through as it is. Either way
            # the SQLSTATE says what happened.
            if _sqlstate(exc) == _QUERY_CANCELED:
                raise StatsQueryTimeout() from exc
            raise

    def _run_dedicated(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        assert self._db is not None
        try:
            with self._db.connection_context():
                return _rows(self._db.execute_sql(sql, params))
        except (OperationalError, InterfaceError) as exc:
            if _sqlstate(exc) == _QUERY_CANCELED:
                raise
            # A connection that died while pooled (a Postgres restart). The
            # driver marks it closed on the failure, so the pool has already
            # dropped it on the way out of the context; try once more on
            # another before giving up.
            log.warning("ai_stats_reader_retry", error=type(exc).__name__)
            try:
                with self._db.connection_context():
                    return _rows(self._db.execute_sql(sql, params))
            except (OperationalError, InterfaceError) as again:
                if _sqlstate(again) == _QUERY_CANCELED:
                    raise
                raise DatabaseUnavailableError() from again

    @staticmethod
    def _run_fallback(sql: str, params: tuple) -> list[dict[str, Any]]:
        # run_db has already checked out the main-pool connection for this
        # thread. SET TRANSACTION must precede the first query of the
        # transaction; SET LOCAL lasts until it ends, so the 30 s default is
        # back for the next statement on this connection.
        with db.atomic():
            db.execute_sql("SET TRANSACTION READ ONLY")
            db.execute_sql(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            return _rows(db.execute_sql(sql, params))

    def _announce(self) -> None:
        if self._announced:
            return
        with self._lock:
            if self._announced:
                return
            self._announced = True
        if self._db is None:
            log.warning(
                "ai_stats_reader_main_pool",
                statement_timeout_ms=STATEMENT_TIMEOUT_MS,
                hint="set ASK_READER_DATABASE_URL to run query_stats as ask_reader (ops/roles.sql)",
            )
        else:
            log.info("ai_stats_reader_ask_reader", max_connections=MAX_CONNECTIONS,
                     statement_timeout_ms=STATEMENT_TIMEOUT_MS)


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of the driver error behind a Peewee one, if any.

    Peewee keeps the psycopg2 exception as `.orig` (and Python as `__context__`);
    psycopg2 carries the five-character code as `pgcode`.
    """
    for candidate in (exc, getattr(exc, "orig", None), exc.__cause__, exc.__context__):
        state = getattr(candidate, "pgcode", None)
        if state:
            return state
    return None


_reader: StatsReader | None = None
_reader_lock = threading.Lock()


def get_reader() -> StatsReader:
    """The process's reader, built from settings on first use."""
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                _reader = StatsReader(settings.ask_reader_database_url)
    return _reader
