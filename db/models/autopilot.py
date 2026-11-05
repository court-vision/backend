"""
usr.autopilot, usr.ai_checkins and usr.ai_decisions -- Dime's check-ins in shadow
(migration 0042, the Dime autopilot plan, step 3; services/ai/checkins.py).

A team Dime runs on has an `Autopilot` row (and an entry in AUTOPILOT_TEAM_IDS).
Each check-in Dime starts itself is an `AiCheckin`, claimed by its insert and
settled with its turn; each call the check-in makes, holds included, is an
`AiDecision`, which the user marks agree or disagree. Backend-only.
"""

from datetime import datetime, timezone

from peewee import (
    BigAutoField, BigIntegerField, BooleanField, DateField, DateTimeField, ForeignKeyField, TextField,
)

from db.base import BaseModel
from db.models.ai_conversations import AiConversation, AiProposal, AiTurn
from db.models.teams import Team
from db.models.users import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Autopilot(BaseModel):
    """One team Dime runs check-ins on. `paused_at` is the Pause switch; `active_since` is
    when it last started or resumed. How Dime may act is added with the acting step."""

    team_id = ForeignKeyField(Team, column_name="team_id", primary_key=True, on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    created_at = DateTimeField(default=_now)
    updated_at = DateTimeField(default=_now)
    active_since = DateTimeField(default=_now)
    paused_at = DateTimeField(null=True)
    paused_by = TextField(null=True)          # user | system
    paused_reason = TextField(null=True)

    class Meta:
        table_name = "autopilot"
        schema = "usr"


class AiCheckin(BaseModel):
    """One turn Dime started itself: `kind` morning | weekly | pre_lock | night, about the game
    day `nba_date`. `state` running | done | failed | missed | skipped."""

    id = BigAutoField(primary_key=True)
    team_id = ForeignKeyField(Team, column_name="team_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    kind = TextField()
    nba_date = DateField()
    manual = BooleanField(default=False)
    due_at = DateTimeField()
    state = TextField(default="running")
    reason = TextField(null=True)
    conversation_id = ForeignKeyField(AiConversation, column_name="conversation_id", null=True,
                                      on_delete="SET NULL")
    turn_id = ForeignKeyField(AiTurn, column_name="turn_id", null=True, on_delete="SET NULL")
    cost_micros = BigIntegerField(default=0)
    started_at = DateTimeField(default=_now)
    finished_at = DateTimeField(null=True)

    class Meta:
        table_name = "ai_checkins"
        schema = "usr"


class AiDecision(BaseModel):
    """One call a check-in made: `area` lineup | streaming | adds_drops | other, `automation`
    (whose plan it reviewed) auto_lineup | stream_lane | scheduled_pickup | none, `call` hold |
    change, and the shadow proposal behind a change. `verdict` is the user's agree / disagree."""

    id = BigAutoField(primary_key=True)
    checkin_id = ForeignKeyField(AiCheckin, column_name="checkin_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    team_id = ForeignKeyField(Team, column_name="team_id", on_delete="CASCADE")
    area = TextField()
    automation = TextField(default="none")
    call = TextField()
    summary = TextField()
    reason = TextField()
    proposal_id = ForeignKeyField(AiProposal, column_name="proposal_id", null=True, on_delete="SET NULL")
    verdict = TextField(null=True)           # agree | disagree
    verdict_at = DateTimeField(null=True)
    created_at = DateTimeField(default=_now)

    class Meta:
        table_name = "ai_decisions"
        schema = "usr"
