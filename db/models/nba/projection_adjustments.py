"""
Projection Adjustments Table
The curated layer of the Court Vision projection: per-player input changes a
box score cannot see — a role after a trade, a return date, a second-year jump.
Written by the dashboard's projections editor. Append-only: an edit supersedes
the previous row, so every judgment call keeps its history.

Three scopes, one live row of each per player per season (migrations 0052, 0053):

- `preseason`: a season-long input to the preseason projection, read by the
  data-platform cv-projection pipeline.
- `season`: a dated note read by the live projection alone (the Live rankings
  plan, step 3): a fact with an end. Its role change (minutes, usage) holds
  until `until_date` or until `depends_on_player` plays again, its return date until
  he plays, and all of it until `review_date`. Nothing is written when a note
  ends: the reader works out what is still in force on its morning.
- `analyst`: the same kind of note, written by the AI analyst from the stored
  news (step 4). In a scope of its own so that a note written by hand is never
  overwritten (where both are read, the one written by hand wins), and so that
  in shadow only the second projection (`cv_live_shadow`) reads it.
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
SCOPE_PRESEASON = "preseason"
SCOPE_SEASON = "season"
SCOPE_ANALYST = "analyst"
SCOPES = (SCOPE_PRESEASON, SCOPE_SEASON, SCOPE_ANALYST)
# The scopes whose rows are notes in season: dated, with a review date
NOTE_SCOPES = (SCOPE_SEASON, SCOPE_ANALYST)
CONFIDENCES = ("confirmed", "likely", "possible")


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
    scope = CharField(max_length=10, default=SCOPE_PRESEASON)
    # A season note's ends: the role change through this day, or until this teammate plays again
    until_date = DateField(null=True)
    depends_on_player = ForeignKeyField(Player, null=True, backref="projection_notes_depending",
                                        on_delete="SET NULL", column_name="depends_on_player_id")
    # The range around return_date; the price uses return_date
    return_earliest = DateField(null=True)
    return_latest = DateField(null=True)
    # What it rests on: [{"type": "news", "id": ..., "source": ..., "headline": ..., "url": ..., "published_at": ...}]
    evidence = BinaryJSONField(null=True)
    confidence = CharField(max_length=10, null=True)
    # Read again by this day: past it a season note is no longer applied
    review_date = DateField(null=True)

    class Meta:
        table_name = "projection_adjustments"
        schema = "nba"

    def __repr__(self) -> str:
        return f"<ProjectionAdjustment(id={self.id}, player_id={self.player_id}, season='{self.season}', kind='{self.kind}')>"

    @classmethod
    def _live(cls, season: str, scope: str):
        return (cls.season == season) & (cls.scope == scope) & cls.superseded_by.is_null() & cls.retired_at.is_null()

    @classmethod
    def active_for(cls, season: str, scope: str = SCOPE_PRESEASON) -> list["ProjectionAdjustment"]:
        """The live row of every player who has one this season in `scope`: the
        preseason adjustments unless the season's notes are asked for."""
        return list(cls.select().where(cls._live(season, scope)))

    @classmethod
    def record(cls, player_id: int, season: str, **fields) -> "ProjectionAdjustment":
        """Make `fields` the player's live row for `season`, in the scope they
        name (a preseason adjustment unless `scope` says a season note).

        Supersedes the current one of that scope when there is one. The order
        is forced by the one-active-row index: reserve the new id, point the
        old row at it, then insert — the deferred foreign key is checked at
        commit.
        """
        db = cls._meta.database
        scope = fields.setdefault("scope", SCOPE_PRESEASON)
        with db.atomic():
            current = cls.get_or_none((cls.player == player_id) & cls._live(season, scope))
            new_id = db.execute_sql("SELECT nextval('nba.projection_adjustments_id_seq')").fetchone()[0]
            if current is not None:
                cls.update(superseded_by=new_id).where(cls.id == current.id).execute()
            cls.insert(id=new_id, player=player_id, season=season, **fields).execute()
        return cls.get_by_id(new_id)

    @classmethod
    def retire(cls, player_id: int, season: str, scope: str = SCOPE_PRESEASON) -> int:
        """Withdraw the player's live row of `scope` without a replacement."""
        return (
            cls.update(retired_at=datetime.utcnow())
            .where((cls.player == player_id) & cls._live(season, scope))
            .execute()
        )
