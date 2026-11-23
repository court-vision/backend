"""
usr.roster_protections -- the players a user protected on a team (migration 0048; the roster
lock, services/roster_protection.py). A protected player is never dropped or suggested as a
drop by Court Vision until the user unprotects him, which deletes the row. Backend-only.
"""

from datetime import datetime, timezone

from peewee import BigAutoField, DateTimeField, ForeignKeyField, IntegerField, TextField

from db.base import BaseModel
from db.models.teams import Team
from db.models.users import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RosterProtection(BaseModel):
    id = BigAutoField(primary_key=True)
    team_id = ForeignKeyField(Team, column_name="team_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    player_id = IntegerField()                 # ESPN, as the boards and the pool key players
    nba_player_id = IntegerField(null=True)
    player_name = TextField()
    player_team = TextField(null=True)
    created_at = DateTimeField(default=_now)

    class Meta:
        table_name = "roster_protections"
        schema = "usr"
