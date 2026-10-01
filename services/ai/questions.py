"""
The router's question log, usr.ai_questions (migration 0026).

Recording never fails a request: a question that can't be logged is still
answered, and the failure is logged instead.

Retention needs no scheduler: text and context older than 90 days are nulled
after each insert, in the same round trip, and by the application itself -- once
when it starts and then once a day for as long as it runs -- so the policy
holds even when nobody asks anything and nothing is deployed. A sweep that
fails is logged and retried by the next one; it never costs the insert it rode
on, or a startup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.logging import get_logger
from db.base import db_operation
from db.models.ai_questions import AiQuestion

RETENTION = timedelta(days=90)
SWEEP_EVERY = timedelta(days=1)


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


async def redact_expired(trigger: str = "startup") -> None:
    """The retention sweep on its own, away from any insert. Never raises."""
    try:
        get_logger().info("ai_questions_redacted", rows=await _sweep(), trigger=trigger)
    except Exception:
        get_logger().exception("ai_question_retention_failed", trigger=trigger)


async def keep_redacting() -> None:
    """The retention sweep for as long as the application runs: at startup, then
    once a day. Startup alone left the policy waiting on the next deploy -- a
    process that stays up with no questions asked would keep text past 90 days.
    Runs until cancelled; a sweep that fails is tried again the next day."""
    trigger = "startup"
    while True:
        await redact_expired(trigger)
        await asyncio.sleep(SWEEP_EVERY.total_seconds())
        trigger = "daily"


@db_operation("ai.question_feedback")
def set_feedback(question_id: int, user_id: int, feedback: Optional[str]) -> bool:
    """Set (or clear) feedback on one of the caller's own questions. False when there
    is no such question *for this caller* -- another user's id reads as missing."""
    updated = (AiQuestion
               .update(feedback=feedback)
               .where(AiQuestion.id == question_id, AiQuestion.user_id == user_id)
               .execute())
    return updated > 0
