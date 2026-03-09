"""
Integration test fixtures.

Requires a running PostgreSQL instance.
Run with: TEST_MARKERS="integration" ./scripts/run_tests.sh

Uses a session-scoped fixture to create schemas and tables once,
then truncates mutable tables between each test for isolation.
"""

import pytest
from peewee import OperationalError

from db.base import db


INTEGRATION_MODELS = []


def _get_models():
    """Lazy-import models to avoid circular import issues at collection time."""
    if INTEGRATION_MODELS:
        return INTEGRATION_MODELS

    from db.models.users import User
    from db.models.verifications import Verification
    from db.models.teams import Team
    from db.models.lineups import Lineup
    from db.models.api_keys import APIKey
    from db.models.pipeline_run import PipelineRun
    from db.models.stats.daily_player_stats import DailyPlayerStats
    from db.models.stats.cumulative_player_stats import CumulativePlayerStats
    from db.models.stats.daily_matchup_score import DailyMatchupScore
    from db.models.stats.rankings import Rankings
    from db.models.nba.players import Player
    from db.models.nba.teams import NBATeam
    from db.models.nba.games import Game
    from db.models.nba.player_game_stats import PlayerGameStats
    from db.models.nba.player_season_stats import PlayerSeasonStats
    from db.models.nba.player_rolling_stats import PlayerRollingStats
    from db.models.nba.player_ownership import PlayerOwnership
    from db.models.nba.player_profiles import PlayerProfile
    from db.models.nba.player_advanced_stats import PlayerAdvancedStats
    from db.models.nba.player_injuries import PlayerInjury
    from db.models.nba.team_stats import TeamStats
    from db.models.nba.live_player_stats import LivePlayerStats
    from db.models.nba.breakout_candidates import BreakoutCandidate
    from db.models.notifications import NotificationPreference, NotificationLog, NotificationTeamPreference

    INTEGRATION_MODELS.extend([
        # User schema
        User, Verification, Team, Lineup, APIKey,
        # Audit
        PipelineRun,
        # Legacy stats
        DailyPlayerStats, CumulativePlayerStats, DailyMatchupScore, Rankings,
        # NBA schema - dimension tables first (FK targets)
        NBATeam, Player,
        # NBA schema - tables that reference NBATeam
        TeamStats,
        # NBA schema - fact/aggregate tables
        PlayerGameStats, PlayerSeasonStats, PlayerRollingStats,
        PlayerOwnership, PlayerProfile, PlayerAdvancedStats,
        Game, PlayerInjury, LivePlayerStats, BreakoutCandidate,
        # Notifications
        NotificationPreference, NotificationLog, NotificationTeamPreference,
    ])
    return INTEGRATION_MODELS


@pytest.fixture(scope="session")
def integration_db():
    """
    Session-scoped: connect to test DB, create schemas and tables.

    Skips the entire session if the database is unavailable.
    """
    try:
        db.connect(reuse_if_open=True)
    except OperationalError as exc:
        pytest.skip(f"Integration DB unavailable: {exc}")

    db.execute_sql("CREATE SCHEMA IF NOT EXISTS nba;")
    db.execute_sql("CREATE SCHEMA IF NOT EXISTS stats_s2;")

    models = _get_models()
    db.create_tables(models, safe=True)

    yield db

    if not db.is_closed():
        db.close()


@pytest.fixture(autouse=True)
def clean_tables(integration_db):
    """Truncate mutable tables between tests for isolation."""
    db.execute_sql("""
        TRUNCATE TABLE
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
