from starlette.middleware.base import BaseHTTPMiddleware
from app.db.base import db

class DatabaseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if db.is_closed():
            db.connect()
        
        try:
            response = await call_next(request)
            return response
        finally:
            if not db.is_closed():
                db.close()

