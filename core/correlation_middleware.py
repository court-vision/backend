"""
Correlation ID Middleware

Injects correlation IDs into requests for distributed tracing.
The correlation ID is propagated through all log entries for the request.
"""

import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logging import set_correlation_id, get_correlation_id


class CorrelationMiddleware(BaseHTTPMiddleware):
    """
    Middleware to inject and propagate correlation IDs.

    - Reads X-Correlation-ID from incoming request headers
    - Generates a new UUID if not present
    - Sets the correlation ID in context for logging
    - Adds X-Correlation-ID to response headers
    """

    HEADER_NAME = "X-Correlation-ID"

    async def dispatch(self, request: Request, call_next) -> Response:
        # Get correlation ID from request header or generate new one
        correlation_id = request.headers.get(self.HEADER_NAME)
        if not correlation_id:
            correlation_id = str(uuid.uuid4())

        # Set in context for logging
        set_correlation_id(correlation_id)

        # Process request
        response = await call_next(request)

        # Add to response headers
        response.headers[self.HEADER_NAME] = correlation_id

        return response
