import asyncio
import os
from typing import Any, Callable, TypeVar

from peewee import Model
from playhouse.db_url import parse
from playhouse.pool import PooledPostgresqlExtDatabase

from core.logging import get_logger

# Get database credentials from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
parsed_url = parse(DATABASE_URL)
db_name = parsed_url.pop('database')

db = PooledPostgresqlExtDatabase(
    db_name,
    max_connections=20,
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
    db.connect()


# Function to close database connection
def close_db():
    """Close database connection."""
    if not db.is_closed():
        db.close()
        log.info("database_connection_closed")


async def run_in_db_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking DB work in a worker thread on that thread's own pooled connection.

    Peewee connection state is thread-local: the request's connection (opened by
    `core.db_middleware` on the event-loop thread) is invisible to
    `asyncio.to_thread` workers. `connection_context()` checks a connection out
    for the call and returns it to the pool afterwards, so nothing leaks.
    """

    def _call() -> T:
        with db.connection_context():
            return fn(*args, **kwargs)

    return await asyncio.to_thread(_call)
