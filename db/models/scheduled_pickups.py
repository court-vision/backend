"""
usr.scheduled_pickups — a free-agent add (optionally with a drop) a user asked
Court Vision to make for a later ESPN day. The executor job claims the rows
that are due and settles them; the writes themselves are audited in
usr.roster_moves under source 'scheduled' (the two audit ids are plain
columns here; the FKs live in the schema, migration 0028).

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py): the backend writes it, data-platform only
reads it — its trigger route runs one EXISTS over the table, and its
freshness check reads `updated_at`.
"""

from datetime import datetime, timezone

from peewee import AutoField, CharField, DateField, DateTimeField, ForeignKeyField, IntegerField, TextField

from db.base import BaseModel
from db.models.teams import Team
from db.models.users import User

STATUSES = ("pending", "executed", "skipped", "failed", "cancelled", "expired")
SETTLED_STATUSES = ("executed", "skipped", "failed", "cancelled", "expired")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ScheduledPickup(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, on_delete="CASCADE", backref="scheduled_pickups")
    team = ForeignKeyField(Team, on_delete="CASCADE", backref="scheduled_pickups")
    add_player_id = IntegerField()                 # ESPN player id
    drop_player_id = IntegerField(null=True)       # ESPN player id; NULL = an open seat
    add_name = CharField(max_length=120)
    add_team = CharField(max_length=8, null=True)
    drop_name = CharField(max_length=120, null=True)
    drop_team = CharField(max_length=8, null=True)
    scoring_period_id = IntegerField()             # ESPN day D
    nba_date = DateField()
    not_before_at = DateTimeField()                # first attempt (UTC)
    deadline_at = DateTimeField(null=True)         # D's first tip-off; NULL = until the day passes
    next_attempt_at = DateTimeField(null=True)     # a deferred retry, or the claim lease
    attempts = IntegerField(default=0)
    last_attempt_at = DateTimeField(null=True)
    status = CharField(max_length=12, default="pending")
    reason = CharField(max_length=40, null=True)
    detail = TextField(null=True)
    audit_id = IntegerField(null=True)             # usr.roster_moves row of the add/drop
    lineup_audit_id = IntegerField(null=True)      # usr.roster_moves row of the seat-on-day-D move
    seated_slot_id = IntegerField(null=True)
    created_at = DateTimeField(default=_now)
    updated_at = DateTimeField(default=_now)
    executed_at = DateTimeField(null=True)

    class Meta:
        table_name = "scheduled_pickups"
        schema = "usr"

    def __repr__(self):
        return (f"<ScheduledPickup(id={self.id}, team={self.team_id}, add={self.add_player_id}, "
                f"day={self.scoring_period_id}, status={self.status})>")
