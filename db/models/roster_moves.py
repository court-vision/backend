"""
usr.roster_moves — every roster write Court Vision sent (or tried to send) to a
provider on a user's behalf: lineup slot moves (manual or automatic) and
add/drop transactions (manual only), told apart by `kind`.

The row is the audit trail and the auto-run dedup: a partial unique index keeps
one *counted* auto attempt (applied / applied_unverified / noop) per team per
day, while rejected / failed rows — and every manual row — may repeat.
Backend-only; data-platform never reads or writes it.
"""

from datetime import datetime

from peewee import AutoField, CharField, DateField, DateTimeField, ForeignKeyField, IntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.teams import Team
from db.models.users import User

SOURCES = ("manual", "auto")
KINDS = ("lineup", "transaction")
STATUSES = ("applied", "applied_unverified", "rejected", "failed", "noop")
COUNTED_AUTO_STATUSES = ("applied", "applied_unverified", "noop")


class RosterMove(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, on_delete="CASCADE", backref="roster_moves")
    team = ForeignKeyField(Team, on_delete="CASCADE", backref="roster_moves")
    nba_date = DateField()
    scoring_period_id = IntegerField(null=True)
    source = CharField(max_length=10)          # manual | auto
    kind = CharField(max_length=12, default="lineup")   # lineup | transaction
    status = CharField(max_length=20)          # applied | applied_unverified | rejected | failed | noop
    # lineup rows: [{player_id, from_slot_id, to_slot_id, role, note}]
    # transaction rows: [{player_id, action: "add" | "drop", name}]
    moves = BinaryJSONField(default=list)
    provider_status = IntegerField(null=True)  # ESPN's HTTP status, when a write was attempted
    error = TextField(null=True)
    idempotency_key = CharField(max_length=96, null=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "roster_moves"
        schema = "usr"

    def __repr__(self):
        return f"<RosterMove(team={self.team_id}, date={self.nba_date}, source={self.source}, status={self.status})>"
