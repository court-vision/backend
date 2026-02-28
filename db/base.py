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
    from .models import User, Verification, Team, Lineup
    from .models.api_keys import APIKey
    from .models.stats.daily_player_stats import DailyPlayerStats
    from .models.stats.cumulative_player_stats import CumulativePlayerStats
    from .models.stats.daily_matchup_score import DailyMatchupScore
    from .models.pipeline_run import PipelineRun

    # Import new normalized NBA schema models
    from .models.nba import (
        Player,
        NBATeam,
        PlayerGameStats,
        PlayerSeasonStats,
        PlayerOwnership,
        PlayerRollingStats,
        TeamStats,
        PlayerProfile,
        PlayerAdvancedStats,
        Game,
        PlayerInjury,
        LivePlayerStats,
        BreakoutCandidate,
    )

    # Import notification models
    from .models.notifications import NotificationPreference, NotificationLog, NotificationTeamPreference

    # Create tables if they don't exist
    # Note: Order matters for foreign key dependencies
    # 1. Dimension tables first (Player, NBATeam)
    # 2. Fact/aggregate tables second
    # 3. Extended data tables last (may reference dimension tables)
    db.create_tables([
        # User schema tables
        User, Verification, Team, Lineup, APIKey,
        # Legacy stats_s2 schema (will be deprecated)
        DailyPlayerStats, CumulativePlayerStats, DailyMatchupScore,
        # NBA schema - audit
        PipelineRun,
        # NBA schema - dimension tables
        Player, NBATeam,
        # NBA schema - team stats (FK to NBATeam)
        TeamStats,
        # NBA schema - fact/aggregate tables
        PlayerGameStats, PlayerSeasonStats, PlayerOwnership, PlayerRollingStats,
        # NBA schema - extended data tables
        PlayerProfile, PlayerAdvancedStats, Game, PlayerInjury,
        # NBA schema - live data
        LivePlayerStats,
        # NBA schema - breakout detection
        BreakoutCandidate,
        # User schema - notification tables
        NotificationPreference, NotificationLog, NotificationTeamPreference,
    ], safe=True)

# Function to close database connection
def close_db():
    """Close database connection."""
    if not db.is_closed():
        db.close()
        print("Database connection closed")