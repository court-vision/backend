"""
Team Stats Table

Season-to-date team statistics combining base per-game counting stats
and advanced efficiency metrics. One row per team per date.

Base stats (per-game): PTS, REB, AST, STL, BLK, TOV, shooting percentages.
Advanced stats: OFF_RATING, DEF_RATING, NET_RATING, PACE, TS%, EFG%, etc.

Used for: player context (pace, team quality), strength-of-schedule analysis,
and opponent defensive rating lookups.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    SmallIntegerField,
    UUIDField,
)

from db.base import BaseModel
from db.models.nba.teams import NBATeam


class TeamStats(BaseModel):
    """
    Season-to-date statistics for an NBA team as of a specific date.

    Attributes:
        id: Auto-incrementing primary key
        team: Foreign key to NBATeam dimension (3-letter abbreviation PK)
        as_of_date: Date these stats are calculated through
        season: Season identifier (e.g., '2025-26')
        gp: Games played
        w, l: Wins and losses
        w_pct: Win percentage
        -- Base Per-Game Stats --
        pts, reb, ast, stl, blk, tov: Per-game averages
        fg_pct, fg3_pct, ft_pct: Shooting percentages
        -- Advanced Metrics --
        off_rating: Points scored per 100 possessions
        def_rating: Points allowed per 100 possessions
        net_rating: off_rating - def_rating
        pace: Possessions per 48 minutes
        ts_pct: True shooting percentage
        efg_pct: Effective field goal percentage
        ast_pct, oreb_pct, dreb_pct, reb_pct: Rate stats
        tov_pct: Team turnover percentage
        pie: Team impact estimate
    """

    id = AutoField(primary_key=True)
    team = ForeignKeyField(
        NBATeam,
        backref="team_stats",
        on_delete="RESTRICT",
        column_name="team_id",
    )
    as_of_date = DateField()
    season = CharField(max_length=7, index=True)

    # Record
    gp = SmallIntegerField(null=True)
    w = SmallIntegerField(null=True)
    l = SmallIntegerField(null=True)
    w_pct = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Per-game counting stats (from Base measure type, PerGame mode)
    pts = DecimalField(max_digits=5, decimal_places=1, null=True)
    reb = DecimalField(max_digits=5, decimal_places=1, null=True)
    ast = DecimalField(max_digits=5, decimal_places=1, null=True)
    stl = DecimalField(max_digits=5, decimal_places=1, null=True)
    blk = DecimalField(max_digits=5, decimal_places=1, null=True)
    tov = DecimalField(max_digits=5, decimal_places=1, null=True)
    fg_pct = DecimalField(max_digits=5, decimal_places=3, null=True)
    fg3_pct = DecimalField(max_digits=5, decimal_places=3, null=True)
    ft_pct = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Efficiency ratings (from Advanced measure type)
    off_rating = DecimalField(max_digits=5, decimal_places=1, null=True)
    def_rating = DecimalField(max_digits=5, decimal_places=1, null=True)
    net_rating = DecimalField(max_digits=5, decimal_places=1, null=True)
    pace = DecimalField(max_digits=5, decimal_places=1, null=True)
    ts_pct = DecimalField(max_digits=5, decimal_places=3, null=True)
    efg_pct = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Rate stats
    ast_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    oreb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    dreb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    reb_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    tov_pct = DecimalField(max_digits=6, decimal_places=3, null=True)
    pie = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "team_stats"
        schema = "nba"
        indexes = (
            # Unique: one row per team per date
            (("team", "as_of_date"), True),
            # Fetch all teams for a date (schedule/matchup analysis)
            (("as_of_date",), False),
        )

    def __repr__(self) -> str:
        return (
            f"<TeamStats("
            f"team_id={self.team_id}, "
            f"date={self.as_of_date}, "
            f"off={self.off_rating}, "
            f"def={self.def_rating}, "
            f"pace={self.pace})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_team_stats(
        cls,
        team_id: str,
        as_of_date,
        season: str,
        stats: dict,
        pipeline_run_id: UUID | None = None,
    ) -> "TeamStats":
        """
        Insert or update season-to-date stats for a team.

        Args:
            team_id: Team abbreviation (e.g., "LAL") — FK to NBATeam
            as_of_date: Date these stats are calculated through
            season: Season identifier (e.g., '2025-26')
            stats: Dictionary with stat values (keys match column names)
            pipeline_run_id: Optional pipeline run UUID

        Returns:
            The created or updated TeamStats instance
        """
        defaults = {
            "season": season,
            "gp": stats.get("gp"),
            "w": stats.get("w"),
            "l": stats.get("l"),
            "w_pct": stats.get("w_pct"),
            "pts": stats.get("pts"),
            "reb": stats.get("reb"),
            "ast": stats.get("ast"),
            "stl": stats.get("stl"),
            "blk": stats.get("blk"),
            "tov": stats.get("tov"),
            "fg_pct": stats.get("fg_pct"),
            "fg3_pct": stats.get("fg3_pct"),
            "ft_pct": stats.get("ft_pct"),
            "off_rating": stats.get("off_rating"),
            "def_rating": stats.get("def_rating"),
            "net_rating": stats.get("net_rating"),
            "pace": stats.get("pace"),
            "ts_pct": stats.get("ts_pct"),
            "efg_pct": stats.get("efg_pct"),
            "ast_pct": stats.get("ast_pct"),
            "oreb_pct": stats.get("oreb_pct"),
            "dreb_pct": stats.get("dreb_pct"),
            "reb_pct": stats.get("reb_pct"),
            "tov_pct": stats.get("tov_pct"),
            "pie": stats.get("pie"),
            "pipeline_run_id": pipeline_run_id,
        }

        record, created = cls.get_or_create(
            team_id=team_id,
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
    def get_latest_for_team(cls, team_id: str) -> "TeamStats | None":
        """Get the most recent stats record for a team."""
        return (
            cls.select()
            .where(cls.team_id == team_id)
            .order_by(cls.as_of_date.desc())
            .first()
        )

    @classmethod
    def get_all_latest(cls) -> list["TeamStats"]:
        """Get the most recent stats record for all 30 teams."""
        from peewee import fn

        latest_date = (
            cls.select(fn.MAX(cls.as_of_date))
            .scalar()
        )
        if not latest_date:
            return []
        return list(
            cls.select()
            .where(cls.as_of_date == latest_date)
            .order_by(cls.off_rating.desc(nulls="last"))
        )
