"""
Integration test fixtures.

Requires a running PostgreSQL instance.
Run with: TEST_MARKERS="integration" ./scripts/run_tests.sh

Uses a session-scoped fixture to build the schema from the real migration
chain once, then truncates mutable tables between each test for isolation.
"""

import os

import pytest
from peewee import OperationalError

from db.base import db
from db.migrate import apply_migrations


@pytest.fixture(scope="session")
def integration_db():
    """
    Session-scoped: connect to the test DB and apply backend/migrations.

    Skips the entire session if the database is unavailable.
    """
    try:
        db.connect(reuse_if_open=True)
    except OperationalError as exc:
        pytest.skip(f"Integration DB unavailable: {exc}")

    apply_migrations(os.environ["DATABASE_URL"])

    yield db

    if not db.is_closed():
        db.close()


@pytest.fixture(autouse=True)
def clean_tables(integration_db):
    """Truncate mutable tables between tests for isolation."""
    db.execute_sql("""
        TRUNCATE TABLE
            usr.lineups,
            usr.teams,
            usr.leagues,
            usr.users,
            nba.live_player_stats,
            nba.breakout_candidates,
            nba.player_injuries,
            nba.player_advanced_stats,
            nba.player_profiles,
            nba.player_ownership,
            nba.player_rolling_stats,
            nba.player_season_stats,
            nba.player_game_stats,
            nba.games,
            nba.players
        RESTART IDENTITY CASCADE
    """)
    yield
