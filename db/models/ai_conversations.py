"""
usr.ai_conversations and usr.ai_turns — the AI agent's chats — and usr.ai_spend,
the ledger its monthly caps sum (migration 0032, docs/AI_AGENT_PLAN.md § 1, § 6).

A turn's `messages` are replayed verbatim by every later turn of its
conversation, so they are written once and never edited; a failed turn has
none and is never replayed. Backend-only; data-platform never reads or writes
these tables.
"""

from datetime import datetime, timezone

from peewee import BigAutoField, BigIntegerField, BooleanField, DateTimeField, ForeignKeyField, IntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.teams import Team
from db.models.users import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AiConversation(BaseModel):
    id = BigAutoField(primary_key=True)
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    team_id = ForeignKeyField(Team, column_name="team_id", null=True, on_delete="SET NULL")
    title = TextField(null=True)
    created_at = DateTimeField(default=_now)
    updated_at = DateTimeField(default=_now)
    busy_until = DateTimeField(null=True)

    class Meta:
        table_name = "ai_conversations"
        schema = "usr"


class AiTurn(BaseModel):
    id = BigAutoField(primary_key=True)
    conversation_id = ForeignKeyField(AiConversation, column_name="conversation_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    created_at = DateTimeField(default=_now)
    message = TextField()
    messages = BinaryJSONField(null=True)
    results = BinaryJSONField(default=dict)
    parts = BinaryJSONField(default=list)
    tool_calls = BinaryJSONField(default=list)
    outcome = TextField()
    model = TextField(null=True)
    model_calls = IntegerField(default=0)
    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)
    cache_read_input_tokens = IntegerField(default=0)
    cache_creation_input_tokens = IntegerField(default=0)
    context_tokens = IntegerField(default=0)
    cost_micros = BigIntegerField(default=0)
    duration_ms = IntegerField(null=True)
    ungrounded_numbers = IntegerField(null=True)
    feedback = TextField(null=True)
    # The user's own note on the answer (migration 0034)
    comment = TextField(null=True)
    comment_at = DateTimeField(null=True)

    class Meta:
        table_name = "ai_turns"
        schema = "usr"


class AiSpend(BaseModel):
    """One turn's reservation, settled to its cost. Cascades from the user only:
    deleting a conversation leaves its spend counted."""

    id = BigAutoField(primary_key=True)
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    turn_id = ForeignKeyField(AiTurn, column_name="turn_id", null=True, on_delete="SET NULL")
    created_at = DateTimeField(default=_now)
    cost_micros = BigIntegerField()
    settled = BooleanField(default=False)

    class Meta:
        table_name = "ai_spend"
        schema = "usr"
