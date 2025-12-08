from utils.constants import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, Depends
from datetime import datetime, timedelta
from jose import jwt, JWTError
from typing import Optional
import random
import bcrypt
import os
import resend

resend.api_key = os.environ.get('RESEND_API_KEY')

# ---------------------- User Authentication ---------------------- #

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# Generate a verification code
def generate_verification_code() -> str:
    return '{:06d}'.format(random.randint(0, 999999))

# Send the verification email
def send_verification_email(to_email: str, code: str) -> dict:
    development_mode = os.environ.get('DEVELOPMENT_MODE', 'false').lower() == 'true'
    
    if not resend.api_key:
        print("RESEND_API_KEY environment variable not set or is empty")
        return {"success": False, "error": "Email service not configured"}
        
    if development_mode:
        print(f"DEVELOPMENT MODE: Would send verification email to {to_email} with code: {code}")
        return {"success": True}
    
    try:
        params: resend.Emails.SendParams = {
            "from": "mail@courtvision.dev",
            "to": [to_email],
            "subject": "Email Verification",
            "html": f"<strong>Please verify your email by entering the following code: {code}</strong>. This code will expire in 5 minutes."
        }
        response = resend.Emails.send(params)
        return {"success": True, "email_id": response.id}
    except Exception as e:
        print(f"Resend API exception: {e}")
        return {"success": False, "error": str(e)}

# Create access token for a user
def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode.update({"exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Verify the access token
def verify_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        print(f"JWT verification failed: {e}")
        return None

# Get the data for the user
def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = verify_access_token(token)
    if payload is None:
        print(f"Token validation failed - token was: {token[:20]}..." if token else "Token validation failed - no token")
        raise HTTPException(status_code=401, detail="Invalid access token")
    return payload

# --------------------- Encryption/Validation --------------------- #

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
