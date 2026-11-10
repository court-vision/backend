"""
Dime in the Draft desk (the Dime autopilot plan, step 7; services/ai/draft.py):
GET/PUT /v1/internal/drafts/{session_id}/dime, POST .../dime/notes and
POST .../dime/seat, and the AI desk's Draft notes, GET /v1/internal/ai/draft-notes.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse


class DimeDraftNote(ApiModel):
    """One note Dime wrote in the room: its words (`text`, null while it runs), the board's numbers
    the server read for it (`facts`), and what it set or made."""

    id: int
    kind: Literal["plan", "pick", "recap"]
    for_pick: Optional[int] = Field(None, description="A pick note: the user's overall pick it is about")
    seat: bool = Field(False, description="A pick Dime made itself, holding the user's seat in a mock")
    state: Literal["running", "done", "failed"]
    reason: Optional[str] = Field(None, description="Why it failed")
    facts: dict[str, Any] = Field(default_factory=dict, description=(
        "pick: pick, picks_until, on_clock, gone_before (the pick `gone` is about), top (three "
        "recommendations: name, position, score, availability, reason...), gone (who won't last), punts, "
        "roster, needs. recap: grade, place, seats, value_over_slot, by_dime. plan: none"))
    text: Optional[str] = None
    plan: Optional[str] = Field(None, description="A plan note: the plan it set")
    punts: Optional[list[str]] = Field(None, description="A plan note: the room's punts it set")
    punts_before: Optional[list[str]] = Field(None, description="A plan note: the room's punts before it")
    took: Optional[dict[str, Any]] = Field(None, description=(
        "A seat note: the pick it recorded (overall_pick, round, player_id, name, position, team, reason)"))
    conversation_id: Optional[int] = Field(None, description="Opens in the AI desk")
    cost_usd: float = 0.0
    created_at: datetime
    finished_at: Optional[datetime] = None


class DimeDraftSeat(ApiModel):
    state: Literal["off", "drafting", "stopping"]
    reason: Optional[str] = Field(None, description=(
        "Why it last stopped: done, stopped, pick_failed, cap_blocked, pool_exhausted, blocked, or an error code"))
    live: bool = Field(False, description="Its loop is still running (one whose process died reads false)")


class DimeDraftRoom(ApiModel):
    enabled: bool = Field(description="DIME_DRAFT_ENABLED (and AI_ENABLED) on the server")
    available: bool = Field(description="enabled, the caller is on the AI desk's list, and their Dime in draft rooms "
                                        "switch is on: the room shows Dime")
    switched_off: bool = Field(False, description="enabled and on the list, but the account's Dime in draft rooms "
                                                  "switch (Account, AI and automation) is off")
    lead: int = Field(description="The room asks for a pick note once the user's pick is this many picks away")
    notes_on: bool = Field(description="A note when each of the user's picks is near")
    seat: DimeDraftSeat
    can_seat: Optional[bool] = Field(None, description="Dime can hold the user's seat here (a mock following nothing)")
    seat_blocker: Optional[str] = Field(None, description="Why it can't, in a line")
    running: Optional[int] = Field(None, description="The note being written now")
    notes: list[DimeDraftNote] = Field(default_factory=list, description="Newest first")


class DimeDraftRoomResp(BaseResponse):
    data: Optional[DimeDraftRoom] = None


class DimeDraftDeskRoom(ApiModel):
    """One of the user's draft rooms, with the notes Dime wrote in it."""

    room_id: int
    name: Optional[str] = Field(None, description="The user's own label for the room")
    kind: str = Field(description="live, manual, mock or import")
    status: str = Field(description="active, completed or abandoned")
    team_id: Optional[int] = None
    last_at: datetime = Field(description="When its newest note was started")
    notes: list[DimeDraftNote] = Field(default_factory=list, description="Newest first")


class DimeDraftDesk(ApiModel):
    enabled: bool = Field(description="DIME_DRAFT_ENABLED (and AI_ENABLED) on the server")
    switched_off: bool = Field(description="The account's Dime in draft rooms switch is off (its notes still list)")
    rooms: list[DimeDraftDeskRoom] = Field(default_factory=list, description="The room with the newest note first")


class DimeDraftDeskResp(BaseResponse):
    data: Optional[DimeDraftDesk] = None


class DimeDraftRoomReq(ApiModel):
    notes: bool = Field(description="A note when each of the user's picks is near")


class DimeDraftNoteReq(ApiModel):
    kind: Literal["plan", "pick", "recap"]


class DimeDraftNoteResp(BaseResponse):
    data: Optional[DimeDraftNote] = None


class DimeDraftSeatReq(ApiModel):
    on: bool = Field(description="true: Dime drafts the user's seat to the end; false: hand it back after this pick")
