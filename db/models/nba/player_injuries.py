"""
Player Injuries Table

Injury status and history for NBA players.
"""

from datetime import datetime, date
from uuid import UUID

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    TextField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player


class PlayerInjury(BaseModel):
    """
    Player injury status snapshots.

    Tracks injury reports over time to understand player availability
    and injury history. Updated multiple times daily during the season.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        report_date: Date of the injury report
        status: Injury status (Out, Doubtful, Questionable, Probable, Available)
        injury_type: Type of injury (e.g., "Knee", "Ankle", "Illness")
        injury_detail: Detailed description of injury
        expected_return: Expected return date (if known)
        pipeline_run_id: Reference to pipeline run
        created_at: When this record was created
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="injuries",
        on_delete="CASCADE",
        column_name="player_id",
    )
    report_date = DateField(index=True)
    status = CharField(max_length=20, index=True)  # Out, Doubtful, Questionable, Probable, Available
    injury_type = CharField(max_length=100, null=True)
    injury_detail = TextField(null=True)
    expected_return = DateField(null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_injuries"
        schema = "nba"
        indexes = (
            (("player", "report_date"), True),  # Unique per player per date
            (("report_date", "status"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerInjury("
            f"player_id={self.player_id}, "
            f"date={self.report_date}, "
            f"status='{self.status}')>"
        )

    @property
    def is_available(self) -> bool:
        """Check if player is available to play."""
        return self.status in ("Available", "Probable")

    @property
    def is_out(self) -> bool:
        """Check if player is definitely out."""
        return self.status == "Out"

    @property
    def is_game_time_decision(self) -> bool:
        """Check if player status is uncertain."""
        return self.status in ("Questionable", "Doubtful")

    @classmethod
    def upsert_injury(
        cls,
        player_id: int,
        report_date: date,
        status: str,
        injury_type: str | None = None,
        injury_detail: str | None = None,
        expected_return: date | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerInjury":
        """
        Insert or update an injury report.

        Args:
            player_id: NBA player ID
            report_date: Date of the report
            status: Injury status
            injury_type: Type of injury
            injury_detail: Detailed description
            expected_return: Expected return date
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerInjury instance
        """
        defaults = {
            "status": status,
            "injury_type": injury_type,
            "injury_detail": injury_detail,
            "expected_return": expected_return,
            "pipeline_run_id": pipeline_run_id,
        }

        injury, created = cls.get_or_create(
            player_id=player_id,
            report_date=report_date,
            defaults=defaults,
        )

        if not created:
            for key, value in defaults.items():
                setattr(injury, key, value)
            injury.save()

        return injury

    @classmethod
    def get_current_status(cls, player_id: int) -> "PlayerInjury | None":
        """
        Get the most recent injury status for a player.

        Args:
            player_id: NBA player ID

        Returns:
            Most recent PlayerInjury record or None
        """
        return (
            cls.select()
            .where(cls.player_id == player_id)
            .order_by(cls.report_date.desc())
            .first()
        )

    @classmethod
    def get_injured_players(cls, report_date: date | None = None) -> list["PlayerInjury"]:
        """
        Get all players with non-Available status.

        Args:
            report_date: Date to check (defaults to today)

        Returns:
            List of PlayerInjury records for injured players
        """
        check_date = report_date or date.today()

        # Get the most recent report for each player on or before the check date
        from peewee import fn

        subquery = (
            cls.select(cls.player_id, fn.MAX(cls.report_date).alias("max_date"))
            .where(cls.report_date <= check_date)
            .group_by(cls.player_id)
        )

        return list(
            cls.select()
            .join(
                subquery,
                on=(
                    (cls.player_id == subquery.c.player_id)
                    & (cls.report_date == subquery.c.max_date)
                ),
            )
            .where(cls.status != "Available")
            .order_by(cls.status, cls.player_id)
        )

    @classmethod
    def get_player_injury_history(
        cls,
        player_id: int,
        limit: int = 10,
    ) -> list["PlayerInjury"]:
        """
        Get injury history for a player.

        Args:
            player_id: NBA player ID
            limit: Maximum records to return

        Returns:
            List of PlayerInjury records ordered by date descending
        """
        return list(
            cls.select()
            .where(cls.player_id == player_id)
            .order_by(cls.report_date.desc())
            .limit(limit)
        )
