from playhouse.pool import PooledPostgresqlDatabase
from peewee import Model
import os

# Get database credentials from environment variables
DB_HOST = os.getenv('DB_HOST', 'caboose.proxy.rlwy.net')
DB_PORT = int(os.getenv('DB_PORT', 33942))
DB_NAME = os.getenv('DB_NAME', 'railway')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'rtVSWuhkROFxOpegJuElubldctbpXmBT')

db = PooledPostgresqlDatabase(
    DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    max_connections=20,
    stale_timeout=300,
    timeout=10,
)

class BaseModel(Model):
    class Meta:
        database = db

# Function to initialize database connection
def init_db():
    """Initialize database connection and create tables if they don't exist."""
    db.connect()
    
    # Import all models to register them
    from .models import User, Verification, Team, Lineup, DailyStats, TotalStats, FreeAgent, Standing
    
    # Create tables if they don't exist
    db.create_tables([
        User, Verification, Team, Lineup,
        DailyStats, TotalStats, FreeAgent, Standing
    ], safe=True)
    
    # print("Database initialized successfully")

# Function to close database connection
def close_db():
    """Close database connection."""
    if not db.is_closed():
        db.close()
        print("Database connection closed")