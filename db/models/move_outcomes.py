"""
usr.move_outcomes — the move ledger: every add and drop a team made, read off
the day-to-day differences in usr.lineup_snapshots, and what it earned in the
box scores afterwards, scored with the league's own scoring (migration 0041).

Keyed by the provider's ids like the snapshots, so a rival's moves are rows
too; `team_id` is the Court Vision team when the move was on one. Derived
data: the backend's services/move_ledger.py rebuilds a league's rows whole each
night, so `updated_at` is the rebuild that wrote the row.

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py): the backend writes it, data-platform only
asks the backend to rebuild it (the move_ledger pipeline, whose target table
this is, for the freshness board).
"""

from datetime import datetime, timezone

from peewee import (
    BigAutoField, BigIntegerField, BooleanField, CharField, DateField, DateTimeField, DecimalField,
    ForeignKeyField, IntegerField, TextField,
)
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.teams import Team

SOURCES = ("you", "scheduled", "lane", "dime", "outside")
FORMATS = ("points", "categories")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MoveOutcome(BaseModel):
    id = BigAutoField(primary_key=True)
    provider = CharField(max_length=16)
    provider_league_id = CharField(max_length=64)
    season = IntegerField()
    provider_team_id = IntegerField()
    team_name = CharField(max_length=120)
    scoring_period_id = IntegerField()          # the first day the move counted
    nba_date = DateField()
    add_player_id = IntegerField(null=True)     # ESPN
    add_nba_id = IntegerField(null=True)
    add_name = CharField(max_length=120, null=True)
    drop_player_id = IntegerField(null=True)    # ESPN
    drop_nba_id = IntegerField(null=True)
    drop_name = CharField(max_length=120, null=True)
    paired = BooleanField()
    source = TextField()                        # you | scheduled | lane | dime | outside
    team_id = ForeignKeyField(Team, column_name="team_id", null=True, on_delete="SET NULL")
    roster_move_id = IntegerField(null=True)
    proposal_id = BigIntegerField(null=True)
    forecast = BinaryJSONField(null=True)
    reason = TextField(null=True)
    format = TextField()                        # points | categories
    windows = BinaryJSONField()
    net_points = DecimalField(max_digits=8, decimal_places=2, null=True)
    updated_at = DateTimeField(default=_now)

    class Meta:
        table_name = "move_outcomes"
        schema = "usr"
