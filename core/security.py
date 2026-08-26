"""
Security utilities module.

MIGRATION NOTE: JWT authentication has been migrated to Clerk.
See core/clerk_auth.py for the new authentication system.

This file retains helper functions (password hashing, email sending)
that may still be used by other parts of the application.
"""

import asyncio
import random
import bcrypt
import os
import resend

from core.logging import get_logger

resend.api_key = os.environ.get('RESEND_API_KEY')

log = get_logger("security")

# ---------------------- DEPRECATED: Custom JWT Auth ---------------------- #
# The following JWT-based authentication code has been replaced by Clerk.
# See core/clerk_auth.py for the new implementation.
#
# from utils.constants import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS
# from fastapi.security import OAuth2PasswordBearer
# from fastapi import HTTPException, Depends
# from datetime import datetime, timedelta
# from jose import jwt, JWTError
# from typing import Optional
#
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
#
# def create_access_token(data: dict) -> str:
#     to_encode = data.copy()
#     to_encode.update({"exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
#     return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
#
# def verify_access_token(token: str) -> Optional[dict]:
#     try:
#         payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
#         return payload
#     except JWTError as e:
#         print(f"JWT verification failed: {e}")
#         return None
#
# def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
#     payload = verify_access_token(token)
#     if payload is None:
#         print(f"Token validation failed - token was: {token[:20]}..." if token else "Token validation failed - no token")
#         raise HTTPException(status_code=401, detail="Invalid access token")
#     return payload
# ---------------------- END DEPRECATED ---------------------- #


# --------------------- Email Utilities --------------------- #
# These may still be used for non-auth emails (notifications, etc.)

def generate_verification_code() -> str:
    """Generate a 6-digit verification code."""
    return '{:06d}'.format(random.randint(0, 999999))


def _send_verification_email_sync(to_email: str, code: str) -> dict:
    """Synchronous helper for sending email (runs in thread pool)."""
    try:
        params: resend.Emails.SendParams = {
            "from": "mail@courtvision.dev",
            "to": [to_email],
            "subject": "Email Verification",
            "html": f"<strong>Please verify your email by entering the following code: {code}</strong>. This code will expire in 5 minutes."
        }
        response = resend.Emails.send(params)
        return {"success": True, "email_id": response['id']}
    except Exception as e:
        log.error("resend_send_failed", error=type(e).__name__, detail=str(e))
        return {"success": False, "error": str(e)}


async def send_verification_email(to_email: str, code: str) -> dict:
    """Send the verification email (async, runs blocking I/O in thread pool)."""
    development_mode = os.environ.get('DEVELOPMENT_MODE', 'false').lower() == 'true'

    if development_mode:
        log.info("verification_email_skipped_development_mode")
        return {"success": True}

    if not resend.api_key:
        log.warning("resend_api_key_missing")
        return {"success": False, "error": "Email service not configured"}

    return await asyncio.to_thread(_send_verification_email_sync, to_email, code)


# --------------------- Password Utilities --------------------- #
# These may still be used for legacy password verification

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def check_password(password: str, hashed_password: str) -> bool:
    """Verify a password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
