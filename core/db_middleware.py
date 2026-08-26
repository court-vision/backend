"""
Per-request database connection.

Peewee's connection state is thread-local, so the connection must be opened
and released on the thread that runs the handlers: the event-loop thread
(all routes are `async def`, and the DB-touching dependencies are too).
Opening it here rather than letting handlers autoconnect means a dead
database is a clean 503 `DATABASE_UNAVAILABLE` envelope instead of a 500.

Blocking DB work that runs in a worker thread (`asyncio.to_thread`) does not
see this connection; wrap it in `db.connection_context()` — see
`db.base.run_in_db_thread`.

`PHYSICALLY_CLOSE` selects `manual_close()` (a fresh connection every request)
over `close()` (return to the pool). It is False: a fresh connection per
request means a DNS lookup + TCP/TLS handshake + auth on the event-loop thread
for every request, and psycopg2's `connect_timeout` does not bound the DNS
step — a hung lookup froze the whole API (2026-08-26). Pooled connections are
recycled by `stale_timeout`, kept honest by TCP keepalives (`db.base`), and a
dead one raises `OperationalError`, which `core.middleware` turns into a 503
and evicts with `manual_close()`.
"""

from __future__ import annotations

from peewee import InterfaceError, OperationalError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logging import get_logger
from core.middleware import render_error
from db.base import db

# No DB needed: liveness, health (which probes on its own thread), docs.
SKIP_PATHS = frozenset({"/health", "/ping", "/", "/docs", "/redoc", "/openapi.json"})

PHYSICALLY_CLOSE = False


def release_connection() -> None:
    if db.is_closed():
        return
    try:
        if PHYSICALLY_CLOSE:
            db.manual_close()
        else:
            db.close()
    except Exception as exc:
        get_logger("db").warning("db_release_failed", error=type(exc).__name__)


class DatabaseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in SKIP_PATHS:
            return await call_next(request)

        try:
            db.connect(reuse_if_open=True)
        except (OperationalError, InterfaceError) as exc:
            return render_error(request, exc)

        try:
            return await call_next(request)
        finally:
            release_connection()
