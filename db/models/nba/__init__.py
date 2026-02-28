"""
NBA Schema Models

Normalized database models for NBA player and game data.
These models replace the denormalized stats_s2 schema.
"""

from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.player_ownership import PlayerOwnership
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.player_advanced_stats import PlayerAdvancedStats
from db.models.nba.games import Game
from db.models.nba.player_injuries import PlayerInjury
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.team_stats import TeamStats
from db.models.nba.breakout_candidates import BreakoutCandidate

__all__ = [
    # Dimension tables
    "Player",
    "NBATeam",
    # Fact/aggregate tables
    "PlayerGameStats",
    "PlayerSeasonStats",
    "PlayerOwnership",
    "PlayerRollingStats",
    # Team stats
    "TeamStats",
    # Extended data tables
    "PlayerProfile",
    "PlayerAdvancedStats",
    "Game",
    "PlayerInjury",
    # Live data
    "LivePlayerStats",
    # Breakout detection
    "BreakoutCandidate",
]
