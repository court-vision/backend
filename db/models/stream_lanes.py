"""
usr.stream_lanes — a standing order for one roster spot: keep it streaming for the
rest of the matchup. Before each day the executor job re-plans the team's lanes
together and, for each spot the plan switches, inserts a scheduled pickup with the
lane's id (migration 0030; several open lanes per team, one per spot, since 0037).
usr.stream_lane_events is the lane's history.

Backend-only; data-platform's trigger route only checks, in SQL, whether a lane's
decision is due.
"""

from datetime import datetime, timezone

from peewee import AutoField, CharField, DateField, DateTimeField, ForeignKeyField, IntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.teams import Team
from db.models.users import User

STATUSES = ("active", "paused", "ended")
OPEN_STATUSES = ("active", "paused")
NOTIFY_MODES = ("email", "desk")
ACTIONS = ("started", "switch", "keep", "blocked", "paused", "resumed", "ended")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StreamLane(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, on_delete="CASCADE", backref="stream_lanes")
    team = ForeignKeyField(Team, on_delete="CASCADE", backref="stream_lanes")
    holder_player_id = IntegerField()              # ESPN player id: who holds the spot now
    holder_name = CharField(max_length=120)
    holder_team = CharField(max_length=8, null=True)
    original_player_id = IntegerField()            # who held it when the lane started
    original_name = CharField(max_length=120)
    start_period = IntegerField()                  # the first ESPN day the lane plans for
    end_period = IntegerField()                    # the matchup's last ESPN day
    end_date = DateField()
    status = CharField(max_length=8, default="active")
    notify = CharField(max_length=8, default="email")
    next_period = IntegerField()                   # the ESPN day the next decision is for
    next_decision_at = DateTimeField(null=True)    # when it is due, or the claim lease
    attempts = IntegerField(default=0)             # every claim bumps it: the fencing token
    reason = CharField(max_length=40, null=True)
    detail = TextField(null=True)
    projection = BinaryJSONField(null=True)        # the plan as of the last decision
    created_at = DateTimeField(default=_now)
    updated_at = DateTimeField(default=_now)
    ended_at = DateTimeField(null=True)

    class Meta:
        table_name = "stream_lanes"
        schema = "usr"


class StreamLaneEvent(BaseModel):
    id = AutoField()
    lane = ForeignKeyField(StreamLane, on_delete="CASCADE", backref="events")
    scoring_period_id = IntegerField(null=True)
    nba_date = DateField(null=True)
    action = CharField(max_length=8)
    add_player_id = IntegerField(null=True)
    add_name = CharField(max_length=120, null=True)
    drop_player_id = IntegerField(null=True)
    drop_name = CharField(max_length=120, null=True)
    projected_player_id = IntegerField(null=True)
    projected_name = CharField(max_length=120, null=True)
    reason = CharField(max_length=40, null=True)
    detail = TextField(null=True)
    pickup_id = IntegerField(null=True)            # usr.scheduled_pickups row of a switch (FK in the schema)
    created_at = DateTimeField(default=_now)

    class Meta:
        table_name = "stream_lane_events"
        schema = "usr"


def active_lane_holders(team_id: int) -> set[int]:
    """The players this team's active lanes hold their spots with: nothing else may drop them
    (scheduled pickups, the desk's transactions)."""
    lanes = StreamLane.select(StreamLane.holder_player_id).where((StreamLane.team == team_id)
                                                                 & (StreamLane.status == "active"))
    return {lane.holder_player_id for lane in lanes}
