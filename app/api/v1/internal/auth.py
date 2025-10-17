from fastapi import APIRouter, Depends
from app.services.auth_service import AuthService
from app.schemas.auth import VerifyEmailReq, CheckCodeReq, UserLoginReq, VerifyEmailResp, CheckCodeResp, UserLoginResp
from app.schemas.common import success_response
from app.core.security import get_current_user

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

@router.get('/verify/auth-check')
async def auth_check(current_user: dict = Depends(get_current_user)):
    return success_response(
        message="Authentication successful",
        data={"user_id": current_user.get("uid"), "email": current_user.get("email")}
    )
