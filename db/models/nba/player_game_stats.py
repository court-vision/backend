"""
Player Game Stats Fact Table

Per-game statistics for NBA players. This is a fact table that stores
one row per player per game played. Replaces the denormalized
stats_s2.daily_player_stats table.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerGameStats(BaseModel):
    """
    Per-game statistics for a player.

    This fact table stores box score statistics for each game a player
    participates in. References the Player and NBATeam dimension tables.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        team: Foreign key to NBATeam dimension (team player played for)
        game_date: Date of the game
        fpts: Fantasy points (calculated)
        pts, reb, ast, stl, blk, tov: Basic counting stats
        min: Minutes played
        fgm, fga, fg3m, fg3a, ftm, fta: Shooting stats
        pipeline_run_id: Reference to the pipeline run that created/updated this record
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="game_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        backref="player_game_stats",
        on_delete="RESTRICT",
        column_name="team_id",
        null=True,  # Allow null for trades/unknown
    )
    game_date = DateField(index=True)

    # Fantasy points (calculated based on league scoring)
    fpts = SmallIntegerField()

    # Basic counting stats
    pts = SmallIntegerField()
    reb = SmallIntegerField()
    ast = SmallIntegerField()
    stl = SmallIntegerField()
    blk = SmallIntegerField()
    tov = SmallIntegerField()
    min = IntegerField()

    # Shooting stats
    fgm = SmallIntegerField()
    fga = SmallIntegerField()
    fg3m = SmallIntegerField()
    fg3a = SmallIntegerField()
    ftm = SmallIntegerField()
    fta = SmallIntegerField()

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_game_stats"
        schema = "nba"
        indexes = (
            # Unique constraint: one row per player per game
            (("player", "game_date"), True),
            # Index for querying by date range
            (("game_date",), False),
            # Index for querying by team
            (("team",), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerGameStats("
            f"player_id={self.player_id}, "
            f"date={self.game_date}, "
            f"fpts={self.fpts})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_game_stats(
        cls,
        player_id: int,
        game_date,
        stats: dict,
        team_id: str | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerGameStats":
        """
        Insert or update game statistics for a player.

        Args:
            player_id: NBA player ID
            game_date: Date of the game
            stats: Dictionary with stat values (pts, reb, ast, etc.)
            team_id: Optional team abbreviation
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerGameStats instance
        """
        defaults = {
            "team": team_id,
            "fpts": stats.get("fpts", 0),
            "pts": stats.get("pts", 0),
            "reb": stats.get("reb", 0),
            "ast": stats.get("ast", 0),
            "stl": stats.get("stl", 0),
            "blk": stats.get("blk", 0),
            "tov": stats.get("tov", 0),
            "min": stats.get("min", 0),
            "fgm": stats.get("fgm", 0),
            "fga": stats.get("fga", 0),
            "fg3m": stats.get("fg3m", 0),
            "fg3a": stats.get("fg3a", 0),
            "ftm": stats.get("ftm", 0),
            "fta": stats.get("fta", 0),
            "pipeline_run_id": pipeline_run_id,
        }

        game_stats, created = cls.get_or_create(
            player_id=player_id,
            game_date=game_date,
            defaults=defaults,
        )

        if not created:
            # Update existing record
            for key, value in defaults.items():
                setattr(game_stats, key, value)
            game_stats.save()

        return game_stats

    @classmethod
    def get_player_games(
        cls,
        player_id: int,
        limit: int = 10,
    ) -> list["PlayerGameStats"]:
        """
        Get recent game stats for a player.

        Args:
            player_id: NBA player ID
            limit: Maximum number of games to return

        Returns:
            List of PlayerGameStats ordered by date descending
        """
        return list(
            cls.select()
            .where(cls.player_id == player_id)
            .order_by(cls.game_date.desc())
            .limit(limit)
        )

    @classmethod
    def get_games_by_date(cls, game_date) -> list["PlayerGameStats"]:
        """
        Get all player stats for a specific date.

        Args:
            game_date: Date to query

        Returns:
            List of all PlayerGameStats for that date
        """
        return list(
            cls.select()
            .where(cls.game_date == game_date)
            .order_by(cls.fpts.desc())
        )
