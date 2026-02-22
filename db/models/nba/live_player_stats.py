"""
Live Player Stats Table

In-progress game statistics for NBA players, updated every ~60 seconds
during live games. This is a continuously-overwritten snapshot table,
separate from the finalized player_game_stats fact table.

One row per player per game, uniquely keyed on (player_id, game_id).
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    IntegerField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player


class LivePlayerStats(BaseModel):
    """
    In-progress game statistics for a player.

    Updated on every live polling cycle (~60s). Stores current box score
    stats for all active games. Records persist until the next game day.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        game_id: NBA game ID (e.g. "0022501234")
        game_date: Date of the game (ET)
        period: Current period (1-4, 5=OT, null if not started)
        game_clock: Current game clock in ISO 8601 duration format ("PT07M23.00S")
        game_status: 1=scheduled, 2=in_progress, 3=final
        fpts: Live fantasy points (calculated)
        pts, reb, ast, stl, blk, tov: Basic counting stats
        min: Minutes played (integer, truncated)
        fgm, fga, fg3m, fg3a, ftm, fta: Shooting stats
        last_updated: Timestamp of last poll that updated this record
        pipeline_run_id: Reference to the pipeline run that last updated this
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="live_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    game_id = CharField(max_length=20)
    game_date = DateField(index=True)
    period = SmallIntegerField(null=True)
    game_clock = CharField(max_length=20, null=True)
    game_status = SmallIntegerField(default=1)  # 1=scheduled, 2=in_progress, 3=final

    # Fantasy points (calculated)
    fpts = SmallIntegerField(default=0)

    # Basic counting stats
    pts = SmallIntegerField(default=0)
    reb = SmallIntegerField(default=0)
    ast = SmallIntegerField(default=0)
    stl = SmallIntegerField(default=0)
    blk = SmallIntegerField(default=0)
    tov = SmallIntegerField(default=0)
    min = IntegerField(default=0)

    # Shooting stats
    fgm = SmallIntegerField(default=0)
    fga = SmallIntegerField(default=0)
    fg3m = SmallIntegerField(default=0)
    fg3a = SmallIntegerField(default=0)
    ftm = SmallIntegerField(default=0)
    fta = SmallIntegerField(default=0)

    # Audit columns
    last_updated = DateTimeField(default=datetime.utcnow)
    pipeline_run_id = UUIDField(null=True, index=True)

    class Meta:
        table_name = "live_player_stats"
        schema = "nba"
        indexes = (
            # Unique constraint: one row per player per game
            (("player", "game_id"), True),
            # Index for querying all live stats for a given date
            (("game_date",), False),
        )

    def __repr__(self) -> str:
        return (
            f"<LivePlayerStats("
            f"player_id={self.player_id}, "
            f"game_id={self.game_id}, "
            f"fpts={self.fpts}, "
            f"status={self.game_status})>"
        )

    @classmethod
    def upsert_live_stats(
        cls,
        player_id: int,
        game_id: str,
        game_date,
        stats: dict,
        pipeline_run_id: UUID | None = None,
    ) -> "LivePlayerStats":
        """
        Insert or update live game statistics for a player.

        Args:
            player_id: NBA player ID
            game_id: NBA game ID
            game_date: Date of the game
            stats: Dictionary with stat values and live game metadata
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated LivePlayerStats instance
        """
        defaults = {
            "game_date": game_date,
            "period": stats.get("period"),
            "game_clock": stats.get("game_clock"),
            "game_status": stats.get("game_status", 1),
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
            "last_updated": datetime.utcnow(),
            "pipeline_run_id": pipeline_run_id,
        }

        live_stats, created = cls.get_or_create(
            player_id=player_id,
            game_id=game_id,
            defaults=defaults,
        )

        if not created:
            for key, value in defaults.items():
                setattr(live_stats, key, value)
            live_stats.save()

        return live_stats

    @classmethod
    def get_live_stats_for_date(cls, game_date) -> list["LivePlayerStats"]:
        """
        Get all live player stats for a specific date, ordered by fpts descending.

        Args:
            game_date: Date to query

        Returns:
            List of LivePlayerStats ordered by fantasy points descending
        """
        return list(
            cls.select()
            .where(cls.game_date == game_date)
            .order_by(cls.fpts.desc())
        )

    @classmethod
    def get_live_stats_for_players(
        cls,
        player_ids: list[int],
        game_date,
    ) -> list["LivePlayerStats"]:
        """
        Get live stats for a specific set of player IDs on a given date.

        Useful for fetching a fantasy team's current live stats.

        Args:
            player_ids: List of NBA player IDs
            game_date: Date to query

        Returns:
            List of LivePlayerStats for the given players
        """
        return list(
            cls.select()
            .where(
                (cls.player_id.in_(player_ids))
                & (cls.game_date == game_date)
            )
        )

    @classmethod
    def get_live_stats_by_names(
        cls,
        names: list[str],
        game_date,
    ) -> list["LivePlayerStats"]:
        """
        Get live stats for players matching a list of display names on a given date.

        Matches using the normalized name (lowercase, stripped) for robustness.
        Used to join ESPN/Yahoo fantasy roster players against live game stats.

        Args:
            names: List of player display names (e.g. from ESPN/Yahoo roster)
            game_date: Date to query

        Returns:
            List of LivePlayerStats with Player pre-loaded via join
        """
        normalized = [n.lower().strip() for n in names]
        return list(
            cls.select(cls, Player)
            .join(Player)
            .where(
                (Player.name_normalized.in_(normalized))
                & (cls.game_date == game_date)
            )
        )
