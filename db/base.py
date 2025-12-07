from playhouse.pool import PooledPostgresqlDatabase
from playhouse.db_url import parse
from peewee import Model
import os

# Get database credentials from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
parsed_url = parse(DATABASE_URL)
db_name = parsed_url.pop('database')

db = PooledPostgresqlDatabase(
    db_name,
    max_connections=20,
    stale_timeout=300,
    **parsed_url
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