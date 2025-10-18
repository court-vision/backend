from datetime import time, datetime, timedelta
from fastapi import APIRouter, Depends
from app.services.auth_service import AuthService
from app.schemas.auth import VerifyEmailReq, CheckCodeReq, UserLoginReq, VerifyEmailResp, CheckCodeResp, UserLoginResp, AuthCheckResp, AuthResponse
from app.schemas.common import ApiStatus
from app.core.security import get_current_user
from app.utils.constants import ACCESS_TOKEN_EXPIRE_DAYS

router = APIRouter(prefix="/auth", tags=["authentication"])

@router.post('/verify/send-email', response_model=VerifyEmailResp)
async def verify_email(req: VerifyEmailReq):
    return await AuthService.send_verification_email(req.email, req.password)

@router.post('/verify/check-code', response_model=CheckCodeResp)
async def check_verification_code(req: CheckCodeReq):
    return await AuthService.check_verification_code(req.email, req.code)

@router.post('/login', response_model=UserLoginResp)
async def login_user(req: UserLoginReq):
    return await AuthService.login_user(req.email, req.password)

@router.get('/verify/auth-check', response_model=AuthCheckResp)
async def auth_check(current_user: dict = Depends(get_current_user)):
    # Check if the token is expired
    if datetime.now() - datetime.fromtimestamp(current_user.get("exp")) > timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS):
        return AuthCheckResp(
            status=ApiStatus.SUCCESS,
            message="Token expired",
            error_code="TOKEN_EXPIRED"
        )
    return AuthCheckResp(
        status=ApiStatus.SUCCESS,
        message="Token is valid",
        error_code="TOKEN_VALID",
        data=AuthResponse(
            access_token=current_user.get("access_token"),
            user_id=current_user.get("uid"),
            email=current_user.get("email"),
            expires_at=(datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)).isoformat()
        )
    )