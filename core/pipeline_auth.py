"""
Pipeline authentication using Bearer token.

This is separate from Clerk auth - used for cron jobs and scheduled tasks.
"""

import os
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

PIPELINE_API_TOKEN = os.getenv("PIPELINE_API_TOKEN")


def verify_pipeline_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """
    Verify the bearer token matches our pipeline secret.

    Raises:
        HTTPException: If token is missing or invalid
    """
    if not PIPELINE_API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: PIPELINE_API_TOKEN not set",
        )

    if credentials.credentials != PIPELINE_API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid pipeline authentication token",
        )

    return credentials.credentials
