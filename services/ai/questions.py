"""
The router's question log, usr.ai_questions (migration 0026).

Recording never fails a request: a question that can't be logged is still
answered, and the failure is logged instead. Retention runs with each insert --
text and context older than 90 days are nulled in the same round trip -- so the
policy needs no scheduler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.logging import get_logger
from db.base import db_operation
from db.models.ai_questions import AiQuestion

RETENTION = timedelta(days=90)


@db_operation("ai.record_question")
def _insert(row: dict[str, Any]) -> int:
    cutoff = datetime.now(timezone.utc) - RETENTION
    (AiQuestion
     .update(question=None, context=None)
     .where(AiQuestion.created_at < cutoff, AiQuestion.question.is_null(False))
     .execute())
    return AiQuestion.insert(**row).execute()


def _storable(value: Any) -> Any:
    """`value` with NUL removed from every string in it, at any depth. Postgres text
    and jsonb cannot hold one, and the model can write one into a tool input or
    its answer -- which would otherwise keep the whole row out of the log."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_storable(key): _storable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_storable(item) for item in value]
    return value


async def record(**row: Any) -> Optional[int]:
    """Insert one question; returns its id, or None if it could not be stored."""
    try:
        return await _insert(_storable(row))
    except Exception:
        get_logger().exception("ai_question_record_failed", outcome=row.get("outcome"))
        return None


@db_operation("ai.question_feedback")
def set_feedback(question_id: int, user_id: int, feedback: Optional[str]) -> bool:
    """Set (or clear) feedback on one of the caller's own questions. False when there
    is no such question *for this caller* -- another user's id reads as missing."""
    updated = (AiQuestion
               .update(feedback=feedback)
               .where(AiQuestion.id == question_id, AiQuestion.user_id == user_id)
               .execute())
    return updated > 0
