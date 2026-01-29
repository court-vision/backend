"""
Player Ownership Table

Tracks ESPN fantasy ownership percentages over time. This allows
for trend analysis of rising/falling player popularity.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    DateField,
    DateTimeField,
    DecimalField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player


class PlayerOwnership(BaseModel):
    """
    ESPN fantasy ownership percentage snapshots.

    This table captures daily ownership percentage from ESPN to track
    trending players (being added/dropped across leagues).

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        snapshot_date: Date of the ownership snapshot
        rost_pct: Roster ownership percentage (0.0000 to 100.0000)
        pipeline_run_id: Reference to the pipeline run that created this record
        created_at: When this record was first created
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="ownership_history",
        on_delete="CASCADE",
        column_name="player_id",
    )
    snapshot_date = DateField(index=True)
    rost_pct = DecimalField(max_digits=7, decimal_places=4)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_ownership"
        schema = "nba"
        indexes = (
            # Unique constraint: one row per player per date
            (("player", "snapshot_date"), True),
            # Index for trending queries
            (("snapshot_date", "rost_pct"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerOwnership("
            f"player_id={self.player_id}, "
            f"date={self.snapshot_date}, "
            f"rost_pct={self.rost_pct})>"
        )

    @classmethod
    def record_ownership(
        cls,
        player_id: int,
        snapshot_date,
        rost_pct: float,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerOwnership":
        """
        Record an ownership snapshot for a player.

        Args:
            player_id: NBA player ID
            snapshot_date: Date of the snapshot
            rost_pct: Ownership percentage
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerOwnership instance
        """
        ownership, created = cls.get_or_create(
            player_id=player_id,
            snapshot_date=snapshot_date,
            defaults={
                "rost_pct": rost_pct,
                "pipeline_run_id": pipeline_run_id,
            },
        )

        if not created and ownership.rost_pct != rost_pct:
            ownership.rost_pct = rost_pct
            ownership.pipeline_run_id = pipeline_run_id
            ownership.save()

        return ownership

    @classmethod
    def get_player_trend(
        cls,
        player_id: int,
        days: int = 7,
    ) -> list["PlayerOwnership"]:
        """
        Get ownership trend for a player over recent days.

        Args:
            player_id: NBA player ID
            days: Number of days to look back

        Returns:
            List of PlayerOwnership records ordered by date
        """
        from datetime import timedelta

        cutoff_date = datetime.utcnow().date() - timedelta(days=days)

        return list(
            cls.select()
            .where(
                (cls.player_id == player_id)
                & (cls.snapshot_date >= cutoff_date)
            )
            .order_by(cls.snapshot_date.asc())
        )

    @classmethod
    def get_trending_up(
        cls,
        days: int = 7,
        min_change: float = 5.0,
        limit: int = 20,
    ) -> list[dict]:
        """
        Get players with rising ownership.

        Args:
            days: Number of days to compare
            min_change: Minimum percentage point increase
            limit: Maximum number of players to return

        Returns:
            List of dicts with player_id and ownership change
        """
        from datetime import timedelta
        from peewee import fn

        today = datetime.utcnow().date()
        past_date = today - timedelta(days=days)

        # Subquery for current ownership
        current = (
            cls.select(cls.player_id, cls.rost_pct.alias("current_pct"))
            .where(cls.snapshot_date == today)
            .alias("current")
        )

        # Subquery for past ownership
        past = (
            cls.select(cls.player_id, cls.rost_pct.alias("past_pct"))
            .where(cls.snapshot_date == past_date)
            .alias("past")
        )

        # This is a simplified version - in practice you'd want a raw SQL query
        # for better performance on this type of comparison
        results = []
        current_data = {
            row.player_id: float(row.rost_pct)
            for row in cls.select().where(cls.snapshot_date == today)
        }
        past_data = {
            row.player_id: float(row.rost_pct)
            for row in cls.select().where(cls.snapshot_date == past_date)
        }

        for player_id, current_pct in current_data.items():
            past_pct = past_data.get(player_id, 0)
            change = current_pct - past_pct
            if change >= min_change:
                results.append({
                    "player_id": player_id,
                    "current_pct": current_pct,
                    "past_pct": past_pct,
                    "change": change,
                })

        # Sort by change descending and limit
        results.sort(key=lambda x: x["change"], reverse=True)
        return results[:limit]
