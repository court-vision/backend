"""
Player Advanced Stats Table

Advanced analytics for NBA players - efficiency ratings, usage, impact metrics.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    SmallIntegerField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerAdvancedStats(BaseModel):
    """
    Advanced statistics for players as of a specific date.

    This table stores efficiency and impact metrics that go beyond
    traditional box score stats. Updated daily during the season.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        team: Current team
        as_of_date: Date these stats are calculated through
        season: Season identifier (e.g., '2024-25')
        gp: Games played
        min: Total minutes
        -- Efficiency Ratings --
        off_rating: Offensive rating (points per 100 possessions)
        def_rating: Defensive rating (points allowed per 100 possessions)
        net_rating: Net rating (off_rating - def_rating)
        -- Shooting Efficiency --
        ts_pct: True shooting percentage
        efg_pct: Effective field goal percentage
        -- Usage & Involvement --
        usg_pct: Usage percentage
        ast_pct: Assist percentage
        ast_to_tov: Assist to turnover ratio
        ast_ratio: Assist ratio
        reb_pct: Rebound percentage
        oreb_pct: Offensive rebound percentage
        dreb_pct: Defensive rebound percentage
        tov_pct: Turnover percentage
        -- Pace & Impact --
        pace: Team pace when player is on court
        pie: Player Impact Estimate
        poss: Possessions played
        -- Plus/Minus --
        plus_minus: Total plus/minus
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="advanced_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        backref="player_advanced_stats",
        on_delete="SET NULL",
        column_name="team_id",
        null=True,
    )
    as_of_date = DateField(index=True)
    season = CharField(max_length=7, index=True)

    # Games and minutes
    gp = SmallIntegerField(null=True)
    min = DecimalField(max_digits=6, decimal_places=1, null=True)

    # Efficiency ratings
    off_rating = DecimalField(max_digits=5, decimal_places=1, null=True)
    def_rating = DecimalField(max_digits=5, decimal_places=1, null=True)
    net_rating = DecimalField(max_digits=5, decimal_places=1, null=True)

    # Shooting efficiency
    ts_pct = DecimalField(max_digits=5, decimal_places=3, null=True)
    efg_pct = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Usage and involvement
    usg_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    ast_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    ast_to_tov = DecimalField(max_digits=5, decimal_places=2, null=True)
    ast_ratio = DecimalField(max_digits=6, decimal_places=3, null=True)
    reb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    oreb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    dreb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    tov_pct = DecimalField(max_digits=6, decimal_places=3, null=True)

    # Pace and impact
    pace = DecimalField(max_digits=5, decimal_places=1, null=True)
    pie = DecimalField(max_digits=5, decimal_places=3, null=True)
    poss = IntegerField(null=True)

    # Plus/minus
    plus_minus = DecimalField(max_digits=6, decimal_places=1, null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_advanced_stats"
        schema = "nba"
        indexes = (
            (("player", "as_of_date"), True),  # Unique per player per date
            (("season", "as_of_date"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerAdvancedStats("
            f"player_id={self.player_id}, "
            f"date={self.as_of_date}, "
            f"net_rating={self.net_rating})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_advanced_stats(
        cls,
        player_id: int,
        as_of_date,
        season: str,
        stats: dict,
        team_id: str | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerAdvancedStats":
        """
        Insert or update advanced stats for a player.

        Args:
            player_id: NBA player ID
            as_of_date: Date these stats are calculated through
            season: Season identifier
            stats: Dictionary with stat values
            team_id: Optional team abbreviation
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerAdvancedStats instance
        """
        defaults = {
            "team": team_id,
            "season": season,
            "gp": stats.get("gp"),
            "min": stats.get("min"),
            "off_rating": stats.get("off_rating"),
            "def_rating": stats.get("def_rating"),
            "net_rating": stats.get("net_rating"),
            "ts_pct": stats.get("ts_pct"),
            "efg_pct": stats.get("efg_pct"),
            "usg_pct": stats.get("usg_pct"),
            "ast_pct": stats.get("ast_pct"),
            "ast_to_tov": stats.get("ast_to_tov"),
            "ast_ratio": stats.get("ast_ratio"),
            "reb_pct": stats.get("reb_pct"),
            "oreb_pct": stats.get("oreb_pct"),
            "dreb_pct": stats.get("dreb_pct"),
            "tov_pct": stats.get("tov_pct"),
            "pace": stats.get("pace"),
            "pie": stats.get("pie"),
            "poss": stats.get("poss"),
            "plus_minus": stats.get("plus_minus"),
            "pipeline_run_id": pipeline_run_id,
        }

        record, created = cls.get_or_create(
            player_id=player_id,
            as_of_date=as_of_date,
            defaults=defaults,
        )

        if not created:
            for key, value in defaults.items():
                if value is not None:
                    setattr(record, key, value)
            record.save()

        return record

    @classmethod
    def get_latest_for_player(cls, player_id: int) -> "PlayerAdvancedStats | None":
        """Get the most recent advanced stats for a player."""
        return (
            cls.select()
            .where(cls.player_id == player_id)
            .order_by(cls.as_of_date.desc())
            .first()
        )
