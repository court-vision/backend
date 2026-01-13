"""
DEPRECATED: Custom authentication endpoints

These endpoints are no longer used - Clerk handles authentication.
Keeping for reference during migration.

To fully remove, also update main.py to remove the auth router import.
"""

from fastapi import APIRouter

# Empty router to prevent import errors in main.py
router = APIRouter(prefix="/auth", tags=["authentication - deprecated"])

# ---------------------- DEPRECATED CODE ---------------------- #
# The following code has been commented out as we now use Clerk for authentication.
# Clerk handles sign-up, sign-in, email verification, and session management.
#
# from fastapi import APIRouter, Depends
# from services.auth_service import AuthService
# from schemas.auth import VerifyEmailReq, CheckCodeReq, UserLoginReq, VerifyEmailResp, CheckCodeResp, UserLoginResp, AuthCheckResp
# from core.security import get_current_user
#
# router = APIRouter(prefix="/auth", tags=["authentication"])
#
# @router.post('/verify/send-email', response_model=VerifyEmailResp)
# async def verify_email(req: VerifyEmailReq):
#     return await AuthService.send_verification_email(req.email, req.password)
#
# @router.post('/verify/check-code', response_model=CheckCodeResp)
# async def check_verification_code(req: CheckCodeReq):
#     return await AuthService.check_verification_code(req.email, req.code)
#
# @router.post('/login', response_model=UserLoginResp)
# async def login_user(req: UserLoginReq):
#     return await AuthService.login_user(req.email, req.password)
#
# @router.get('/verify/auth-check', response_model=AuthCheckResp)
# async def auth_check(current_user: dict = Depends(get_current_user)):
#     return await AuthService.auth_check(current_user)
# ---------------------- END DEPRECATED ---------------------- #
