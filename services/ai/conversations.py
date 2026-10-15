"""
The AI agent's conversations and turns, usr.ai_conversations / usr.ai_turns
(migration 0032, docs/AI_AGENT_PLAN.md § 1).

Two rules hold everything else up:

- **History is append-only.** A turn's `messages` are written once, with its
  row, and every later turn replays the successful turns' messages in order,
  unedited. Claude Opus 5.5 rejects a replayed thinking block whose earlier
  history changed, and the prompt cache only reads back an unchanged prefix.
- **One turn at a time per conversation.** A turn claims its conversation
  (`busy_until`) before it starts and releases it when its row is written. A
  claim that is never released (a crashed process) lapses on its own.

Every function scopes by `user_id`: another user's conversation reads as missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from peewee import fn

from core.settings import settings
from db.base import db, db_operation
from db.models.ai_conversations import AiConversation, AiTurn
from services.ai import spend

TITLE_LENGTH = 80
OK = "ok"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def storable(value: Any) -> Any:
    """`value` with NUL removed from every string in it, at any depth: Postgres text
    and jsonb cannot hold one, and a model can write one."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {storable(key): storable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [storable(item) for item in value]
    return value


def title_of(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1].rstrip() + "…"


@db_operation("ai.create_conversation")
def create(user_id: int, team_id: Optional[int]) -> AiConversation:
    return AiConversation.create(user_id=user_id, team_id=team_id)


@db_operation("ai.list_conversations")
def list_for(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    counts = dict(AiTurn.select(AiTurn.conversation_id, fn.COUNT(AiTurn.id))
                  .where(AiTurn.user_id == user_id)
                  .group_by(AiTurn.conversation_id)
                  .tuples())
    rows = (AiConversation.select()
            .where(AiConversation.user_id == user_id)
            .order_by(AiConversation.updated_at.desc(), AiConversation.id.desc())
            .limit(limit))
    return [{"conversation": row, "turns": counts.get(row.id, 0)} for row in rows]


@db_operation("ai.get_conversation")
def get(conversation_id: int, user_id: int) -> Optional[AiConversation]:
    return AiConversation.get_or_none(AiConversation.id == conversation_id, AiConversation.user_id == user_id)


@db_operation("ai.conversation_turns")
def turns_of(conversation_id: int, user_id: int) -> list[AiTurn]:
    return list(AiTurn.select()
                .where(AiTurn.conversation_id == conversation_id, AiTurn.user_id == user_id)
                .order_by(AiTurn.id))


@db_operation("ai.delete_conversation")
def delete(conversation_id: int, user_id: int) -> bool:
    """The conversation and its turns. Their spend stays in usr.ai_spend."""
    return AiConversation.delete().where(
        AiConversation.id == conversation_id, AiConversation.user_id == user_id).execute() > 0


@db_operation("ai.claim_conversation")
def claim(conversation_id: int, user_id: int, hold: timedelta) -> bool:
    """Take the conversation for one turn. False while another turn holds it."""
    now = _now()
    return (AiConversation
            .update(busy_until=now + hold, updated_at=now)
            .where(AiConversation.id == conversation_id,
                   AiConversation.user_id == user_id,
                   (AiConversation.busy_until.is_null()) | (AiConversation.busy_until < now))
            .execute()) > 0


@db_operation("ai.release_conversation")
def release(conversation_id: int) -> None:
    AiConversation.update(busy_until=None).where(AiConversation.id == conversation_id).execute()


@dataclass
class History:
    """What a new turn starts from: the replayed messages, the tool results its
    display blocks may point at, and how full the conversation is."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    turns: int = 0
    context_tokens: int = 0

    @property
    def full(self) -> bool:
        return (self.turns >= settings.ai_agent_max_turns
                or self.context_tokens >= settings.ai_agent_max_context_tokens)


@db_operation("ai.conversation_history")
def history(conversation_id: int, user_id: int) -> History:
    """The successful turns, in order. A failed turn left no messages, and the
    model never saw its results, so it contributes nothing."""
    out = History()
    rows = (AiTurn.select(AiTurn.messages, AiTurn.results, AiTurn.context_tokens)
            .where(AiTurn.conversation_id == conversation_id, AiTurn.user_id == user_id,
                   AiTurn.outcome == OK, AiTurn.messages.is_null(False))
            .order_by(AiTurn.id))
    for row in rows:
        out.messages.extend(row.messages)
        out.results.update(row.results or {})
        out.turns += 1
        out.context_tokens = row.context_tokens
    return out


@db_operation("ai.record_turn")
def _insert(conversation_id: int, row: dict[str, Any], title: Optional[str], reservation_id: Optional[int]) -> int:
    with db.atomic():
        turn_id = AiTurn.insert(conversation_id=conversation_id, **row).execute()
        if reservation_id is not None:
            spend.settle_sync(reservation_id, row.get("cost_micros", 0), turn_id)
        AiConversation.update(updated_at=_now(), busy_until=None).where(AiConversation.id == conversation_id).execute()
        if title:
            # The first message names the conversation; a later one never renames it
            (AiConversation.update(title=title)
             .where(AiConversation.id == conversation_id, AiConversation.title.is_null())
             .execute())
    return turn_id


async def record(conversation_id: int, row: dict[str, Any], *, title: Optional[str] = None,
                 reservation_id: Optional[int] = None) -> int:
    """Write one turn, settle its spend reservation to its cost, and release the
    conversation, together: the next turn must not start until this one's
    messages are part of the history."""
    return await _insert(conversation_id, storable(row), title, reservation_id)


@db_operation("ai.turn_comment")
def set_comment(turn_id: int, user_id: int, comment: Optional[str]) -> bool:
    return (AiTurn.update(comment=comment, comment_at=_now() if comment else None)
            .where(AiTurn.id == turn_id, AiTurn.user_id == user_id)
            .execute()) > 0


@db_operation("ai.turn_feedback")
def set_feedback(turn_id: int, user_id: int, feedback: Optional[str]) -> bool:
    return (AiTurn.update(feedback=feedback)
            .where(AiTurn.id == turn_id, AiTurn.user_id == user_id)
            .execute()) > 0
