from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from .common import BaseRequest, BaseResponse, UserResponse, AuthResponse

# ------------------------------- User Management Models ------------------------------- #

#                          ------- Incoming -------                           #

class UserCreateReq(BaseRequest):
    email: EmailStr
    password: str = Field(min_length=8, description="Password must be at least 8 characters")

class UserUpdateReq(BaseRequest):
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=8, description="Password must be at least 8 characters")

class UserDeleteReq(BaseRequest):
    password: str

#                          ------- Outgoing -------                           #

class UserCreateResp(BaseResponse):
    """User creation response with authentication data"""
    data: Optional[AuthResponse] = None

class UserDeleteResp(BaseResponse):
    """User deletion response"""
    data: Optional[dict] = None

class UserUpdateResp(BaseResponse):
    """User update response"""
    data: Optional[UserResponse] = None
