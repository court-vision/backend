"""
usr.ai_draft_rooms and usr.ai_draft_notes -- Dime in the Draft desk (migration 0046,
the Dime autopilot plan, step 7; services/ai/draft.py).

An `AiDraftRoom` is Dime's state in one draft room: whether it writes a note when
each of the user's picks is near (`notes`), and whether it holds the user's seat in
a mock (`seat`: off, drafting, stopping). An `AiDraftNote` is one note Dime wrote in
a room (a plan, a pick, a recap), one agent turn each: claimed by its insert,
settled with the turn's answer, its cost, and what it set (a plan's punts) or made
(the pick a seat note recorded). Backend-only.
"""

from datetime import datetime, timezone

from peewee import BigAutoField, BigIntegerField, BooleanField, DateTimeField, ForeignKeyField, IntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.ai_conversations import AiConversation, AiTurn
from db.models.drafts import DraftSession
from db.models.users import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AiDraftRoom(BaseModel):
    """Dime in one room: `notes` on, a note when each of the user's picks is near; `seat`
    drafting, Dime makes the user's picks in this mock (stopping: after the one it is making)."""

    session_id = ForeignKeyField(DraftSession, column_name="session_id", primary_key=True, on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    notes = BooleanField(default=False)
    seat = TextField(default="off")           # off | drafting | stopping
    seat_reason = TextField(null=True)
    created_at = DateTimeField(default=_now)
    updated_at = DateTimeField(default=_now)

    class Meta:
        table_name = "ai_draft_rooms"
        schema = "usr"


class AiDraftNote(BaseModel):
    """One note: `kind` plan | pick | recap; a pick note is about the user's pick `for_pick`,
    and `seat` when Dime made that pick itself. `facts` are the board's numbers it read."""

    id = BigAutoField()
    session_id = ForeignKeyField(DraftSession, column_name="session_id", on_delete="CASCADE")
    user_id = ForeignKeyField(User, column_name="user_id", on_delete="CASCADE")
    kind = TextField()
    for_pick = IntegerField(null=True)
    seat = BooleanField(default=False)
    state = TextField(default="running")      # running | done | failed
    reason = TextField(null=True)
    facts = BinaryJSONField(default=dict)
    text = TextField(null=True)
    plan = TextField(null=True)
    punts = BinaryJSONField(null=True)
    punts_before = BinaryJSONField(null=True)
    took = BinaryJSONField(null=True)
    conversation_id = ForeignKeyField(AiConversation, column_name="conversation_id", null=True, on_delete="SET NULL")
    turn_id = ForeignKeyField(AiTurn, column_name="turn_id", null=True, on_delete="SET NULL")
    cost_micros = BigIntegerField(default=0)
    created_at = DateTimeField(default=_now)
    finished_at = DateTimeField(null=True)

    class Meta:
        table_name = "ai_draft_notes"
        schema = "usr"
