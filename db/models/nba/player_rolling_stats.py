"""
Player Rolling Stats Table

Per-game averages over a fixed day window (L7, L14, L30). One row per
player per date per window. The `window_days` column acts as a discriminator
so all three windows live in the same table.

Stats are per-game averages; shooting percentages are computed from
window totals (sum FGM / sum FGA) rather than averaged per-game.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    SmallIntegerField,
    UUIDField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerRollingStats(BaseModel):
    """
    Rolling per-game averages for a player over a fixed day window.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        team: Current team as of this date (nullable for recently traded players)
        as_of_date: Date through which the window is calculated
        window_days: Window length in calendar days (7, 14, or 30)
        gp: Games played within the window
        fpts: Avg fantasy points per game
        pts, reb, ast, stl, blk, tov, min: Avg counting stats per game
        fgm, fga, fg_pct: Avg makes/attempts per game + window FG%
        fg3m, fg3a, fg3_pct: Same for 3-pointers
        ftm, fta, ft_pct: Same for free throws
        pipeline_run_id: Reference to the pipeline run that wrote this record
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="rolling_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        backref="player_rolling_stats",
        on_delete="RESTRICT",
        column_name="team_id",
        null=True,
    )
    as_of_date = DateField()
    window_days = SmallIntegerField()  # 7, 14, or 30

    # Games played within the window
    gp = SmallIntegerField()

    # Per-game averages
    fpts = DecimalField(max_digits=6, decimal_places=2)
    pts = DecimalField(max_digits=5, decimal_places=2)
    reb = DecimalField(max_digits=5, decimal_places=2)
    ast = DecimalField(max_digits=5, decimal_places=2)
    stl = DecimalField(max_digits=5, decimal_places=2)
    blk = DecimalField(max_digits=5, decimal_places=2)
    tov = DecimalField(max_digits=5, decimal_places=2)
    min = DecimalField(max_digits=5, decimal_places=2)

    # Shooting averages (per game)
    fgm = DecimalField(max_digits=5, decimal_places=2)
    fga = DecimalField(max_digits=5, decimal_places=2)
    # Window FG% = sum(fgm) / sum(fga)
    fg_pct = DecimalField(max_digits=5, decimal_places=4)

    fg3m = DecimalField(max_digits=5, decimal_places=2)
    fg3a = DecimalField(max_digits=5, decimal_places=2)
    fg3_pct = DecimalField(max_digits=5, decimal_places=4)

    ftm = DecimalField(max_digits=5, decimal_places=2)
    fta = DecimalField(max_digits=5, decimal_places=2)
    ft_pct = DecimalField(max_digits=5, decimal_places=4)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_rolling_stats"
        schema = "nba"
        indexes = (
            # Unique: one row per player per date per window
            (("player", "as_of_date", "window_days"), True),
            # For fetching all players on a date for a given window (rankings)
            (("as_of_date", "window_days"), False),
            # For fetching a player's rolling history across dates
            (("player", "window_days"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerRollingStats("
            f"player_id={self.player_id}, "
            f"date={self.as_of_date}, "
            f"window={self.window_days}d, "
            f"gp={self.gp}, "
            f"fpts={self.fpts})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_rolling_stats(
        cls,
        player_id: int,
        as_of_date,
        window_days: int,
        gp: int,
        stats: dict,
        team_id: str | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerRollingStats":
        """
        Insert or update rolling averages for a player/date/window combination.

        Args:
            player_id: NBA player ID
            as_of_date: Date through which the window is calculated
            window_days: Window length (7, 14, or 30)
            gp: Games played within the window
            stats: Dict of per-game average stats and shooting percentages
            team_id: Optional current team abbreviation
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated PlayerRollingStats instance
        """
        defaults = {
            "team": team_id,
            "gp": gp,
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
            "fg_pct": stats.get("fg_pct", 0),
            "fg3m": stats.get("fg3m", 0),
            "fg3a": stats.get("fg3a", 0),
            "fg3_pct": stats.get("fg3_pct", 0),
            "ftm": stats.get("ftm", 0),
            "fta": stats.get("fta", 0),
            "ft_pct": stats.get("ft_pct", 0),
            "pipeline_run_id": pipeline_run_id,
        }

        record, created = cls.get_or_create(
            player_id=player_id,
            as_of_date=as_of_date,
            window_days=window_days,
            defaults=defaults,
        )

        if not created:
            for key, value in defaults.items():
                setattr(record, key, value)
            record.save()

        return record

    @classmethod
    def get_latest_for_window(
        cls,
        window_days: int,
    ) -> tuple[object, list["PlayerRollingStats"]]:
        """
        Get the most recent rolling stats records for a given window.

        Returns a (latest_date, records) tuple. Records are joined with
        the Player dimension so player.name is accessible.

        Args:
            window_days: Window length (7, 14, or 30)

        Returns:
            Tuple of (latest_date, list of PlayerRollingStats with player joined)
        """
        latest_date = (
            cls.select(cls.as_of_date)
            .where(cls.window_days == window_days)
            .order_by(cls.as_of_date.desc())
            .limit(1)
            .scalar()
        )

        if not latest_date:
            return None, []

        records = list(
            cls.select(cls, Player)
            .join(Player)
            .where(
                (cls.as_of_date == latest_date) & (cls.window_days == window_days)
            )
            .order_by(cls.fpts.desc())
        )

        return latest_date, records
