from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, AuthResponse, VerificationResponse

# ------------------------------- Authentication Models ------------------------------- #

#                          ------- Incoming -------                           #

class VerifyEmailReq(BaseRequest):
    email: str
    password: str

class CheckCodeReq(BaseRequest):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, description="Verification code must be 6 digits")

class UserLoginReq(BaseRequest):
    email: EmailStr
    password: str

#                          ------- Outgoing -------                           #

class VerifyEmailResp(BaseResponse):
    """Email verification request response"""
    data: Optional[VerificationResponse] = None

class CheckCodeResp(BaseResponse):
    """Email verification code check response"""
    data: Optional[AuthResponse] = None

class UserLoginResp(BaseResponse):
    """User login response with authentication data"""
    data: Optional[AuthResponse] = None

class AuthCheckResp(BaseResponse):
    """Authentication check response"""
    expired: bool = False
    data: Optional[AuthResponse] = None