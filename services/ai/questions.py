"""
The router's question log, usr.ai_questions (migration 0026).

Recording never fails a request: a question that can't be logged is still
answered, and the failure is logged instead.

Retention needs no scheduler: text and context older than 90 days are nulled
after each insert, in the same round trip, and once more when the application
starts -- so the policy holds even when nobody asks anything. A sweep that
fails is logged and retried by the next one; it never costs the insert it rode
on, or a startup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.logging import get_logger
from db.base import db_operation
from db.models.ai_questions import AiQuestion

RETENTION = timedelta(days=90)


def _redact_expired() -> int:
    cutoff = datetime.now(timezone.utc) - RETENTION
    return (AiQuestion
            .update(question=None, context=None)
            .where(AiQuestion.created_at < cutoff, AiQuestion.question.is_null(False))
            .execute())


@db_operation("ai.record_question")
def _insert(row: dict[str, Any]) -> int:
    # The insert first, and on its own: statements here autocommit, so nothing
    # the sweep does afterwards can take the row back.
    question_id = AiQuestion.insert(**row).execute()
    try:
        _redact_expired()
    except Exception:
        get_logger().exception("ai_question_retention_failed", trigger="insert")
    return question_id


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


@db_operation("ai.redact_expired_questions")
def _sweep() -> int:
    return _redact_expired()


async def redact_expired() -> None:
    """The retention sweep on its own, for application startup. Never raises."""
    try:
        get_logger().info("ai_questions_redacted", rows=await _sweep(), trigger="startup")
    except Exception:
        get_logger().exception("ai_question_retention_failed", trigger="startup")


@db_operation("ai.question_feedback")
def set_feedback(question_id: int, user_id: int, feedback: Optional[str]) -> bool:
    """Set (or clear) feedback on one of the caller's own questions. False when there
    is no such question *for this caller* -- another user's id reads as missing."""
    updated = (AiQuestion
               .update(feedback=feedback)
               .where(AiQuestion.id == question_id, AiQuestion.user_id == user_id)
               .execute())
    return updated > 0
