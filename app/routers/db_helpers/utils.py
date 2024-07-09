from ..constants import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, Depends
from datetime import datetime, timedelta
from contextlib import contextmanager
from jose import jwt, JWTError
from typing import Optional
import psycopg2
import bcrypt

# ---------------------- User Authentication ---------------------- #

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

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

# ---------------------- Database Connection ---------------------- #


# Connect to the PostgreSQL database
def connect_to_db() -> psycopg2.connect:
    conn = psycopg2.connect(
        user="jameslk3",
        password="REDACTED",
        host="cv-db.postgres.database.azure.com",
        port="5432",
        database="cv-db"
    )
    return conn

conn = connect_to_db()


# Get the cursor for the database and close it when done
@contextmanager
def get_cursor():
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()

# -------------------------- Encryption --------------------------- #

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

# --------------------------- Testing ----------------------------- #
