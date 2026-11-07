"""
The user's AI features, for the whole account (usr.ai_settings, migration 0044; the Dime
autopilot plan's "AI features and the autopilot switch", step 5). Apart from the
automations table: these aren't moves, so they have no levels.

    memory       Dime keeps notes and reads them back (services/ai/memory.py). Off, the
                 memory tools stay in the agent's tool list (the cached prefix doesn't
                 change) and answer that memory is off, and the header's digest is empty.
                 The user's own "What Dime knows" still reads and edits the notes.
    draft_rooms  Dime's side of the Draft desk. Only stored and served here: the Draft
                 desk's Dime (step 7) reads it.

No row reads as both on. A team's autopilot switch coming on turns both on
(services/ai/levels.py); going off leaves them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from db.base import db_operation
from db.models.autopilot import AiSettings as AiSettingsRow

FEATURES = ("memory", "draft_rooms")


@dataclass(frozen=True)
class AiSettings:
    memory: bool = True
    draft_rooms: bool = True

    def view(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT = AiSettings()


def of_row(row: Any) -> AiSettings:
    if row is None:
        return DEFAULT
    return AiSettings(memory=bool(row.memory), draft_rooms=bool(row.draft_rooms))


@db_operation("ai.settings_get")
def _get(user_id: int) -> AiSettings:
    return of_row(AiSettingsRow.get_or_none(AiSettingsRow.user_id == user_id))


async def get(user_id: int) -> AiSettings:
    """The user's features (both on when they never changed one)."""
    return await _get(user_id)


@db_operation("ai.settings_set")
def _set(user_id: int, changes: dict[str, bool], now: datetime) -> AiSettings:
    current = of_row(AiSettingsRow.get_or_none(AiSettingsRow.user_id == user_id))
    values = {**current.view(), **changes}
    (AiSettingsRow.insert(user_id=user_id, created_at=now, updated_at=now, **values)
     .on_conflict(conflict_target=[AiSettingsRow.user_id],
                  update={**{getattr(AiSettingsRow, k): v for k, v in changes.items()}, AiSettingsRow.updated_at: now})
     .execute())
    return of_row(AiSettingsRow.get(AiSettingsRow.user_id == user_id))


async def update(user_id: int, *, memory: Optional[bool] = None, draft_rooms: Optional[bool] = None,
                 now: Optional[datetime] = None) -> AiSettings:
    """Change the features given (None leaves one as it is); returns them as they now stand."""
    changes = {k: v for k, v in (("memory", memory), ("draft_rooms", draft_rooms)) if v is not None}
    if not changes:
        return await get(user_id)
    return await _set(user_id, changes, now or datetime.now(timezone.utc))
