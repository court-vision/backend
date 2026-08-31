"""
Player Projections Table

Preseason per-game stat projections (ESPN's published projections now, Court
Vision's own later). One row per (player, season, source, as_of_date) snapshot;
the latest as_of_date is the current projection. Written by the data-platform
preseason-market pipeline; backend reads.
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
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.nba.players import Player


class PlayerProjection(BaseModel):
    """
    Projected per-game stat line for a season.

    The stat columns share names with nba.player_rolling_stats so a row can feed
    StatLine.from_row directly. Rate stats (FG%/FT%/3P%) are recomputed from
    makes/attempts at read time, never stored.
    """

    # Stat columns, in StatLine.ROW_KEYS order (minus gp, which is projected_gp here)
    STAT_KEYS = ("pts", "reb", "ast", "stl", "blk", "tov",
                 "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "min")

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="projections",
        on_delete="CASCADE",
        column_name="player_id",
    )
    season = CharField(max_length=7, index=True)  # e.g., '2026-27'
    source = CharField(max_length=16, default="espn")
    as_of_date = DateField()
    projected_gp = SmallIntegerField(null=True)

    min = DecimalField(max_digits=5, decimal_places=2, null=True)
    pts = DecimalField(max_digits=5, decimal_places=2, null=True)
    reb = DecimalField(max_digits=5, decimal_places=2, null=True)
    ast = DecimalField(max_digits=5, decimal_places=2, null=True)
    stl = DecimalField(max_digits=5, decimal_places=2, null=True)
    blk = DecimalField(max_digits=5, decimal_places=2, null=True)
    tov = DecimalField(max_digits=5, decimal_places=2, null=True)
    fgm = DecimalField(max_digits=5, decimal_places=2, null=True)
    fga = DecimalField(max_digits=5, decimal_places=2, null=True)
    fg3m = DecimalField(max_digits=5, decimal_places=2, null=True)
    fg3a = DecimalField(max_digits=5, decimal_places=2, null=True)
    ftm = DecimalField(max_digits=5, decimal_places=2, null=True)
    fta = DecimalField(max_digits=5, decimal_places=2, null=True)

    raw = BinaryJSONField(null=True)  # provider payload extras (source-specific)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_projections"
        schema = "nba"
        indexes = (
            # One snapshot per player per season per source per date
            (("player", "season", "source", "as_of_date"), True),
            # Latest-snapshot queries
            (("season", "as_of_date"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerProjection("
            f"player_id={self.player_id}, "
            f"season='{self.season}', "
            f"source='{self.source}', "
            f"as_of={self.as_of_date})>"
        )

    @classmethod
    def record_projection(
        cls,
        player_id: int,
        season: str,
        as_of_date,
        line: dict,
        projected_gp: int | None = None,
        raw: dict | None = None,
        source: str = "espn",
        pipeline_run_id: UUID | None = None,
    ) -> "PlayerProjection":
        """
        Record a projection snapshot. `line` is keyed by STAT_KEYS names; missing
        keys stay NULL. Re-running the same day updates the existing row.
        """
        stats = {key: line.get(key) for key in cls.STAT_KEYS}
        projection, created = cls.get_or_create(
            player_id=player_id,
            season=season,
            source=source,
            as_of_date=as_of_date,
            defaults={
                **stats,
                "projected_gp": projected_gp,
                "raw": raw,
                "pipeline_run_id": pipeline_run_id,
            },
        )

        if not created:
            for key, value in stats.items():
                setattr(projection, key, value)
            projection.projected_gp = projected_gp
            projection.raw = raw
            projection.pipeline_run_id = pipeline_run_id
            projection.save()

        return projection

    @classmethod
    def latest_for_season(cls, season: str, source: str = "espn") -> list["PlayerProjection"]:
        """Every player's row from the most recent snapshot date for `season`."""
        latest = (
            cls.select(cls.as_of_date)
            .where((cls.season == season) & (cls.source == source))
            .order_by(cls.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if latest is None:
            return []
        return list(
            cls.select().where(
                (cls.season == season) & (cls.source == source) & (cls.as_of_date == latest)
            )
        )
