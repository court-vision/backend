from datetime import datetime, timedelta
from typing import Optional
from app.core.security import (
    hash_password, check_password, create_access_token, 
    send_verification_email, generate_verification_code
)
from app.schemas.auth import VerifyEmailResp, CheckCodeResp, UserLoginResp
from app.schemas.user import UserCreateResp
from app.schemas.common import ApiStatus, AuthResponse, VerificationResponse
from app.db.models import User, Verification
from app.utils.constants import ACCESS_TOKEN_EXPIRE_DAYS
import time

class AuthService:
    
    @staticmethod
    async def send_verification_email(email: str, password: str) -> VerifyEmailResp:
        try:
            hashed_password = hash_password(password)
        except Exception as validation_error:
            print(f"Validation error: {validation_error}")
            return VerifyEmailResp(
                status=ApiStatus.VALIDATION_ERROR,
                message="Invalid request data",
                error_code="VALIDATION_ERROR"
            )

        # Check if the email is already in use
        try:
            verification_data = Verification.select().where(Verification.email == email).first()
            
            if verification_data:
                if time.time() - verification_data.timestamp < 300:
                    return VerifyEmailResp(
                        status=ApiStatus.SUCCESS,
                        message="Verification email already sent recently",
                        data=VerificationResponse(
                            verification_sent=True,
                            email=email,
                            expires_in_seconds=300 - int(time.time() - verification_data.timestamp)
                        )
                    )
                else:
                    verification_data.delete_instance()

            user_exists = User.select().where(User.email == email).exists()
            
            if user_exists:
                print("Already exists in users")
                return VerifyEmailResp(
                    status=ApiStatus.CONFLICT,
                    message="Email address is already registered",
                    error_code="EMAIL_ALREADY_EXISTS"
                )
            
            # Generate the verification code
            code = generate_verification_code()
            
            # Send the verification email FIRST before creating the database record
            res = send_verification_email(email, code)
            if not res.get("success"):
                print(f"Error in verify_email 1: {res.get('error')}")
                return VerifyEmailResp(
                    status=ApiStatus.SERVER_ERROR,
                    message="Failed to send verification email",
                    error_code="EMAIL_SEND_FAILED"
                )
            
            # Only create the verification record if email sending was successful
            Verification.create(
                email=email,
                code=code,
                hashed_password=hashed_password,
                timestamp=int(time.time()),
                type="email"
            )
            
            return VerifyEmailResp(
                status=ApiStatus.SUCCESS,
                message="Verification email sent successfully",
                data=VerificationResponse(
                    verification_sent=True,
                    email=email,
                    expires_in_seconds=300
                )
            )
                
        except Exception as e:
            print(f"Error in verify_email 2: {e}")
            return VerifyEmailResp(
                status=ApiStatus.SERVER_ERROR,
                message="Internal server error during email verification",
                error_code="INTERNAL_ERROR"
            )

    @staticmethod
    async def check_verification_code(email: str, code: str) -> CheckCodeResp:
        try:
            verification_data = Verification.select().where(Verification.email == email).first()
            if not verification_data:
                return CheckCodeResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No verification request found for this email",
                    error_code="VERIFICATION_NOT_FOUND"
                )
            
            if verification_data.code != code or time.time() - verification_data.timestamp > 300:
                return CheckCodeResp(
                    status=ApiStatus.ERROR,
                    message="Invalid or expired verification code",
                    error_code="INVALID_VERIFICATION_CODE"
                )
            
            # Delete the verification data
            verification_data.delete_instance()
            
            resp = AuthService.create_user(email, verification_data.hashed_password)
            if resp.success:
                return CheckCodeResp(
                    status=ApiStatus.SUCCESS,
                    message="Account created successfully",
                    data=AuthResponse(
                        access_token=resp.access_token,
                        email=email,
                        expires_at=(datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)).isoformat()
                    )
                )
            else:
                return CheckCodeResp(
                    status=ApiStatus.SERVER_ERROR,
                    message="Failed to create account",
                    error_code="ACCOUNT_CREATION_FAILED"
                )
            
        except Exception as e:
            print(f"Error in check_verification_code: {e}")
            return CheckCodeResp(
                status=ApiStatus.SERVER_ERROR,
                message="Internal server error during verification",
                error_code="INTERNAL_ERROR"
            )

    @staticmethod
    def create_user(email: str, hashed_password: str) -> UserCreateResp:
        try:
            user_exists = User.select().where(User.email == email).exists()
            
            if user_exists:
                return UserCreateResp(access_token=None, already_exists=True, success=True, valid=True)
            
            user = User.create(
                email=email,
                password=hashed_password,
                created_at=datetime.now()
            )
            
            access_token = create_access_token({"uid": user.user_id, "email": email, "exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
            
            return UserCreateResp(access_token=access_token, already_exists=False, success=True, valid=True)
            
        except Exception as e:
            print(f"Error in create_user: {e}")
            return UserCreateResp(access_token=None, already_exists=False, success=False, valid=False)

    @staticmethod
    async def login_user(email: str, password: str) -> UserLoginResp:
        try:
            user_data = User.select().where(User.email == email).first()

            if not user_data or not check_password(password, user_data.password):
                return UserLoginResp(
                    status=ApiStatus.AUTHENTICATION_ERROR,
                    message="Invalid email or password",
                    error_code="INVALID_CREDENTIALS"
                )
                
            access_token = create_access_token({"uid": user_data.user_id, "email": email})

            return UserLoginResp(
                status=ApiStatus.SUCCESS,
                message="Login successful",
                data=AuthResponse(
                    access_token=access_token,
                    user_id=user_data.user_id,
                    email=email,
                    expires_at=(datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)).isoformat()
                )
            )
            
        except Exception as e:
            print(f"Error in login_user: {e}")
            return UserLoginResp(
                status=ApiStatus.SERVER_ERROR,
                message="Internal server error during login",
                error_code="INTERNAL_ERROR"
            )
