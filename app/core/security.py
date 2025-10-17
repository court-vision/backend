from app.utils.constants import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, Depends
from datetime import datetime, timedelta
from jose import jwt, JWTError
from mailersend import MailerSendClient, EmailBuilder
from typing import Optional
import random
import bcrypt
import os

# ---------------------- User Authentication ---------------------- #

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# Generate a verification code
def generate_verification_code() -> str:
    return '{:06d}'.format(random.randint(0, 999999))

# Send the verification email
def send_verification_email(to_email: str, code: str) -> dict:
    mailersend_api_key = os.environ.get('MAILERSEND_API_TOKEN')
    development_mode = os.environ.get('DEVELOPMENT_MODE', 'false').lower() == 'true'
    
    if not mailersend_api_key:
        print("SENDGRID_API_KEY environment variable not set")
        return {"success": False, "error": "Email service not configured"}
        
    if development_mode:
        print(f"DEVELOPMENT MODE: Would send verification email to {to_email} with code: {code}")
        return {"success": True}

    ms = MailerSendClient(api_key=mailersend_api_key)
    email = (EmailBuilder()
        .from_email("mail@courtvision.dev", "Court Vision")
        .to(to_email, to_email)
        .subject("Email Verification")
        .html(f"<strong>Please verify your email by entering the following code: {code}</strong>")
        .build()
    )

    try:
        response = ms.emails.send(email)
        return {"success": True, "email_id": response.data.id}
    except Exception as e:
        print(f"Mailersend API exception: {e}")
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
    except JWTError:
        return None

# Get the data for the user
def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = verify_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid access token")
    return payload

# --------------------- Encryption/Validation --------------------- #

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
