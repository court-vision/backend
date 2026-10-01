"""
usr.ai_questions — one row per question the AI router sent to the model
(migration 0026, docs/AI_PHASE1_PLAN.md § 5).

It is the roadmap as much as an audit trail: `statmuse` rows are stat questions
no Court Vision view covers, `cannot` rows are questions nothing covers. A row
with gap `invalid_target` is a routing bug: its `target` is the destination the
server refused and `missing` says why ("rejected: player_id"). The question
text and context are nulled after 90 days; the counts stay.
Backend-only; data-platform never reads or writes it.
"""

from datetime import datetime, timezone

from peewee import BigAutoField, DateTimeField, ForeignKeyField, IntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.users import User

KINDS = ("show", "statmuse", "cannot")
FEEDBACK = ("up", "down")


class AiQuestion(BaseModel):
    id = BigAutoField(primary_key=True)
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))
    question = TextField(null=True)
    context = BinaryJSONField(null=True)
    kind = TextField(null=True)
    target = BinaryJSONField(null=True)
    statmuse_query = TextField(null=True)
    gap = TextField(null=True)
    missing = TextField(null=True)
    tool_calls = BinaryJSONField(default=list)
    outcome = TextField()
    model_calls = IntegerField(default=0)
    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)
    cache_read_input_tokens = IntegerField(default=0)
    cache_creation_input_tokens = IntegerField(default=0)
    duration_ms = IntegerField(null=True)
    ungrounded_numbers = IntegerField(null=True)
    feedback = TextField(null=True)

    class Meta:
        table_name = "ai_questions"
        schema = "usr"
