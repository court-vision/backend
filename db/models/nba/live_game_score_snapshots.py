"""
Live Game Score Snapshots Table (backend read-only mirror)

Periodic snapshots of NBA game scores written by the data-platform
LiveGameStatsPipeline every ~60 seconds. The backend reads this table
to serve the score-over-time chart and as a fallback when the live
box score API is temporarily unavailable.
"""

from datetime import datetime, timedelta

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    UUIDField,
)

from db.base import BaseModel


class LiveGameScoreSnapshot(BaseModel):
    """
    Score snapshot for an NBA game at a specific point in time.

    Read-only from the backend's perspective — data-platform is the writer.
    """

    id = AutoField(primary_key=True)
    game_id = CharField(max_length=20, index=True)
    game_date = DateField(index=True)
    home_team = CharField(max_length=10)
    away_team = CharField(max_length=10)
    home_score = SmallIntegerField(default=0)
    away_score = SmallIntegerField(default=0)
    period = SmallIntegerField(null=True)
    game_clock = CharField(max_length=20, null=True)
    game_status = SmallIntegerField(default=1)  # 1=scheduled, 2=in_progress, 3=final
    captured_at = DateTimeField(default=datetime.utcnow, index=True)
    pipeline_run_id = UUIDField(null=True)

    class Meta:
        table_name = "live_game_score_snapshots"
        schema = "nba"
        indexes = (
            (("game_id", "captured_at"), False),
        )

    @classmethod
    def get_snapshots_for_game(cls, game_id: str) -> list["LiveGameScoreSnapshot"]:
        """
        Get all score snapshots for a game, ordered chronologically.

        Args:
            game_id: NBA game ID

        Returns:
            List of LiveGameScoreSnapshot ordered by captured_at ascending
        """
        return list(
            cls.select()
            .where(cls.game_id == game_id)
            .order_by(cls.captured_at.asc())
        )

    @classmethod
    def get_latest_for_game(cls, game_id: str) -> "LiveGameScoreSnapshot | None":
        """
        Get the most recent snapshot for a game.

        Used as a fallback when the live box score API is temporarily unavailable
        (e.g. between quarters, pre-tip-off). If the snapshot is recent and
        game_status == 2, the game is still live.

        Args:
            game_id: NBA game ID

        Returns:
            Most recent snapshot or None if no snapshots exist
        """
        return (
            cls.select()
            .where(cls.game_id == game_id)
            .order_by(cls.captured_at.desc())
            .first()
        )

    @classmethod
    def is_game_live(cls, game_id: str, staleness_minutes: int = 5) -> bool:
        """
        Check if a game is currently live based on snapshot data.

        Returns True if the most recent snapshot has game_status=2 and was
        captured within the last `staleness_minutes` minutes.

        Args:
            game_id: NBA game ID
            staleness_minutes: How recent the snapshot must be to trust it

        Returns:
            True if game appears to be in progress
        """
        latest = cls.get_latest_for_game(game_id)
        if not latest:
            return False
        if latest.game_status != 2:
            return False
        cutoff = datetime.utcnow() - timedelta(minutes=staleness_minutes)
        return latest.captured_at >= cutoff
