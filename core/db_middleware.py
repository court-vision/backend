import asyncio

from starlette.middleware.base import BaseHTTPMiddleware
from db.base import db


class DatabaseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if db.is_closed():
            await asyncio.to_thread(db.connect)

        try:
            response = await call_next(request)
            return response
        finally:
            if not db.is_closed():
                # Use manual_close() instead of close() to physically close the
                # connection rather than returning it to the pool. This prevents
                # stale connections (e.g., after a DB restart) from being reused
                # on the next request.
                try:
                    await asyncio.to_thread(db.manual_close)
                except Exception:
                    pass
