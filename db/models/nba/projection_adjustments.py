"""
Projection Adjustments Table
The curated layer of the Court Vision projection: per-player input changes a
box score cannot see — a role after a trade, a return date, a second-year jump.
Read by the data-platform cv-projection pipeline, written by the dashboard's
projections editor. Append-only: an edit supersedes the previous row, so every
judgment call keeps its history.
"""

from datetime import datetime

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    SmallIntegerField,
    TextField,
)
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.nba.players import Player

ADJUSTMENT_KINDS = ("year2", "trade", "role", "injury_return", "injury_current", "age", "other")


class ProjectionAdjustment(BaseModel):
    """
    One judgment about one player's season. Minutes and games are absolute
    targets, not deltas: re-running the projection, or ESPN moving its line,
    cannot compound them. `usage` scales the scoring and playmaking stats
    together (pts, fga, fta, fg3a, ast, tov) so the shooting splits stay
    coherent; `rates` scales single stats.
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(Player, backref="projection_adjustments", on_delete="CASCADE", column_name="player_id")
    season = CharField(max_length=7)
    kind = CharField(max_length=16)
    minutes = DecimalField(max_digits=4, decimal_places=1, null=True)
    games = SmallIntegerField(null=True)
    return_date = DateField(null=True)
    usage = DecimalField(max_digits=4, decimal_places=3, null=True)
    rates = BinaryJSONField(null=True)
    note = TextField()
    source_url = TextField(null=True)
    author = CharField(max_length=64)
    created_at = DateTimeField(default=datetime.utcnow)
    superseded_by = ForeignKeyField("self", null=True, backref="supersedes", column_name="superseded_by")
    retired_at = DateTimeField(null=True)

    class Meta:
        table_name = "projection_adjustments"
        schema = "nba"

    def __repr__(self) -> str:
        return f"<ProjectionAdjustment(id={self.id}, player_id={self.player_id}, season='{self.season}', kind='{self.kind}')>"

    @classmethod
    def active_for(cls, season: str) -> list["ProjectionAdjustment"]:
        """The live adjustment of every player who has one this season."""
        return list(
            cls.select().where(
                (cls.season == season) & cls.superseded_by.is_null() & cls.retired_at.is_null()
            )
        )

    @classmethod
    def record(cls, player_id: int, season: str, **fields) -> "ProjectionAdjustment":
        """Make `fields` the player's live adjustment for `season`.

        Supersedes the current one when there is one. The order is forced by
        the one-active-row index: reserve the new id, point the old row at it,
        then insert — the deferred foreign key is checked at commit.
        """
        db = cls._meta.database
        with db.atomic():
            current = cls.get_or_none(
                (cls.player == player_id) & (cls.season == season)
                & cls.superseded_by.is_null() & cls.retired_at.is_null()
            )
            new_id = db.execute_sql("SELECT nextval('nba.projection_adjustments_id_seq')").fetchone()[0]
            if current is not None:
                cls.update(superseded_by=new_id).where(cls.id == current.id).execute()
            cls.insert(id=new_id, player=player_id, season=season, **fields).execute()
        return cls.get_by_id(new_id)

    @classmethod
    def retire(cls, player_id: int, season: str) -> int:
        """Withdraw the player's live adjustment without a replacement."""
        return (
            cls.update(retired_at=datetime.utcnow())
            .where(
                (cls.player == player_id) & (cls.season == season)
                & cls.superseded_by.is_null() & cls.retired_at.is_null()
            )
            .execute()
        )
