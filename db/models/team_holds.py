"""
usr.team_holds -- Dime's steering inputs into the automations (migration 0043, the
Dime autopilot plan, step 4; services/steering.py).

A hold is one player and a run of days: keep (no lane drops him, nor Dime), block
(no lane adds him, nor Dime), override (his value on those days, as a share of his
usual one) or pin (auto-lineup's fill leaves him where he is). Each carries the
reason it was set and is read only through `ends_on`. Backend-only.
"""

from datetime import date, datetime, timezone

from peewee import (
    BigAutoField, DateField, DateTimeField, DecimalField, ForeignKeyField, IntegerField, TextField,
)

from db.base import BaseModel
from db.models.ai_conversations import AiProposal
from db.models.teams import Team
from db.models.users import User

KINDS = ("keep", "block", "override", "pin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TeamHold(BaseModel):
    id = BigAutoField(primary_key=True)
    team_id = ForeignKeyField(Team, column_name="team_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    kind = TextField()
    player_id = IntegerField()                 # ESPN, as the boards and the pool key players
    nba_player_id = IntegerField(null=True)
    player_name = TextField()
    player_team = TextField(null=True)
    share = DecimalField(max_digits=3, decimal_places=2, null=True, auto_round=True)   # override only
    starts_on = DateField()
    ends_on = DateField()
    reason = TextField()
    proposal_id = ForeignKeyField(AiProposal, column_name="proposal_id", null=True, on_delete="SET NULL")
    applied_by = TextField(default="user")     # user | dime
    created_at = DateTimeField(default=_now)
    cleared_at = DateTimeField(null=True)
    cleared_by = TextField(null=True)          # user | dime

    class Meta:
        table_name = "team_holds"
        schema = "usr"


def live_holds(team_id: int, today: date) -> list[TeamHold]:
    """The team's holds still in force on `today` or later, by start day: cleared ones and
    those whose last day has passed are left out, so a hold never outlives its end date."""
    return list(TeamHold.select()
                .where((TeamHold.team_id == team_id) & TeamHold.cleared_at.is_null(True)
                       & (TeamHold.ends_on >= today))
                .order_by(TeamHold.starts_on, TeamHold.id))
