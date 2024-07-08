import bcrypt
import psycopg2
from contextlib import contextmanager


# ---------------------- Database Connection ---------------------- #

conn = None

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

def get_connection() -> psycopg2.connect:
    global conn
    if conn is None:
        conn = connect_to_db()
    return conn

# Get the cursor for the database and close it when done
@contextmanager
def get_cursor():
    cur = get_connection().cursor()
    try:
        yield cur
    finally:
        cur.close()

# -------------------------- Encryption --------------------------- #

# Encrypt a string
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

# Check if a password matches the hash
def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))