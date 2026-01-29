"""
Player Season Stats Aggregate Table

Aggregated season statistics for NBA players. This table stores
cumulative totals computed from player_game_stats. Replaces the
denormalized stats_s2.cumulative_player_stats table.
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
    DecimalField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerSeasonStats(BaseModel):
    """
    Aggregated season statistics for a player.

    This table stores cumulative statistics for the season up to a
    specific date. Used for rankings, comparisons, and trend analysis.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        team: Current team (may change mid-season due to trades)
        as_of_date: Date these stats are calculated through
        season: Season identifier (e.g., '2024-25')
        gp: Games played
        fpts: Total fantasy points
        pts, reb, ast, stl, blk, tov: Cumulative counting stats
        min: Total minutes played
        fgm, fga, fg3m, fg3a, ftm, fta: Cumulative shooting stats
        rank: Fantasy ranking (1 = best)
        rost_pct: ESPN roster ownership percentage
        pipeline_run_id: Reference to the pipeline run that created/updated this record
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="season_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        backref="player_season_stats",
        on_delete="RESTRICT",
        column_name="team_id",
        null=True,
    )
    as_of_date = DateField(index=True)
    season = CharField(max_length=7, index=True)  # e.g., '2024-25'

    # Games played
    gp = SmallIntegerField()

    # Fantasy points (cumulative)
    fpts = IntegerField()

    # Basic counting stats (cumulative)
    pts = IntegerField()
    reb = IntegerField()
    ast = IntegerField()
    stl = SmallIntegerField()
    blk = SmallIntegerField()
    tov = SmallIntegerField()
    min = IntegerField()

    # Shooting stats (cumulative)
    fgm = IntegerField()
    fga = IntegerField()
    fg3m = IntegerField()
    fg3a = IntegerField()
    ftm = IntegerField()
    fta = IntegerField()

    # Rankings and ownership
    rank = SmallIntegerField(null=True, index=True)
    rost_pct = DecimalField(max_digits=7, decimal_places=4, null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_season_stats"
        schema = "nba"
        indexes = (
            # Unique constraint: one row per player per date
            (("player", "as_of_date"), True),
            # Index for getting latest stats
            (("season", "as_of_date"), False),
            # Index for rankings
            (("as_of_date", "rank"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerSeasonStats("
            f"player_id={self.player_id}, "
            f"date={self.as_of_date}, "
            f"gp={self.gp}, "
            f"fpts={self.fpts})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @property
    def fpts_per_game(self) -> float:
        """Calculate fantasy points per game."""
        if self.gp and self.gp > 0:
            return round(self.fpts / self.gp, 1)
        return 0.0

    @property
    def ppg(self) -> float:
        """Calculate points per game."""
        if self.gp and self.gp > 0:
            return round(self.pts / self.gp, 1)
        return 0.0

    @property
    def rpg(self) -> float:
        """Calculate rebounds per game."""
        if self.gp and self.gp > 0:
            return round(self.reb / self.gp, 1)
        return 0.0

    @property
    def apg(self) -> float:
        """Calculate assists per game."""
        if self.gp and self.gp > 0:
            return round(self.ast / self.gp, 1)
        return 0.0

    @classmethod
    def upsert_season_stats(
        cls,
        player_id: int,
        as_of_date,
        season: str,
        stats: dict,
        team_id: str | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerSeasonStats":
        """
        Insert or update season statistics for a player.

        Args:
            player_id: NBA player ID
            as_of_date: Date these stats are calculated through
            season: Season identifier (e.g., '2024-25')
            stats: Dictionary with cumulative stat values
            team_id: Optional current team abbreviation
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerSeasonStats instance
        """
        defaults = {
            "team": team_id,
            "season": season,
            "gp": stats.get("gp", 0),
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
            "rank": stats.get("rank"),
            "rost_pct": stats.get("rost_pct"),
            "pipeline_run_id": pipeline_run_id,
        }

        season_stats, created = cls.get_or_create(
            player_id=player_id,
            as_of_date=as_of_date,
            defaults=defaults,
        )

        if not created:
            # Update existing record
            for key, value in defaults.items():
                setattr(season_stats, key, value)
            season_stats.save()

        return season_stats

    @classmethod
    def get_latest_rankings(
        cls,
        season: str,
        limit: int = 100,
    ) -> list["PlayerSeasonStats"]:
        """
        Get the latest player rankings for a season.

        Args:
            season: Season identifier (e.g., '2024-25')
            limit: Maximum number of players to return

        Returns:
            List of PlayerSeasonStats ordered by rank
        """
        # Get the most recent date with data for this season
        latest_date = (
            cls.select(cls.as_of_date)
            .where(cls.season == season)
            .order_by(cls.as_of_date.desc())
            .limit(1)
            .scalar()
        )

        if not latest_date:
            return []

        return list(
            cls.select()
            .where((cls.season == season) & (cls.as_of_date == latest_date))
            .order_by(cls.rank.asc(nulls="last"))
            .limit(limit)
        )
