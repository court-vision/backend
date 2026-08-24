from playhouse.pool import PooledPostgresqlExtDatabase
from playhouse.db_url import parse
from peewee import Model
import os

# Get database credentials from environment variables
DATABASE_URL = os.getenv('DATABASE_URL')
parsed_url = parse(DATABASE_URL)
db_name = parsed_url.pop('database')

db = PooledPostgresqlExtDatabase(
    db_name,
    max_connections=20,
    stale_timeout=300,
    **parsed_url
)

class BaseModel(Model):
    class Meta:
        database = db

def init_db():
    """Apply pending schema migrations, then open the connection pool.

    Schema is owned by backend/migrations (see migrations/README.md); Peewee
    models describe tables but never create them.
    """
    from .migrate import apply_migrations

    apply_migrations(DATABASE_URL)
    db.connect()

# Function to close database connection
def close_db():
    """Close database connection."""
    if not db.is_closed():
        db.close()
        print("Database connection closed")