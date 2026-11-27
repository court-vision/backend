"""
Dime in the Draft desk (usr.ai_draft_rooms / usr.ai_draft_notes, migration 0046;
the Dime autopilot plan, step 7).

The board already does a good drafter's arithmetic: it values every player by the
league's scoring, scores each recommendation with its parts, and says who is likely
gone before the user's next pick. Dime sits beside it and does what a drafter does
with that board:

- **pick**: Dime's pick for the user's next pick, the one on the clock included.
  The room asks once that pick is `NOTE_LEAD` picks away or nearer (James,
  2026-11-26: no switch in the room, and no plan or note to read), and once more
  when the player Dime chose is drafted before the user picks (`RETAKES`). Dime
  records one player and one line on why for this roster with `recommend_pick`
  (the note's `pick`), and the board highlights him with that line. The board's
  numbers are read by the server first and put in the brief, so it costs one
  model call in the usual case (`facts` keeps them).
- **plan**: a plan the draft is played to, with `set_draft_plan` setting the
  room's punts. The Draft lab no longer asks for one (2026-11-26); a room that
  has one keeps it, and the seat still drafts to it.
- **seat**: in a mock room Dime can hold the user's seat. The seat loop runs the
  other seats to the user's turn with the autopicker, then Dime makes the pick
  with `draft_pick`, through the room's own pick path (recorded `dime`), and so
  on to the end. Only a mock: in a live ESPN draft Court Vision records picks and
  writes none, so there Dime advises.
- **recap**: after the draft, Dime reads the recap (`get_draft_recap`, a read the
  AI desk has too) and, in a real draft, saves what the season starts from and
  league-mates' habits to its notes.
- **chat**: the room's Dime chat (2026-11-26, step 2 of the rework): the AI desk's
  agent and tools in one conversation per room (kind `room`), where the user asks
  anything about the draft, a plan included; each message carries the room's state
  (`chat_line`). The board's Ask Dime puts a question in its box; the user sends it.

Every note is one agent turn in its own conversation (kind `draft`), started from
the room by the user (the button, the notes switch, or the seat), so it spends on
the AI desk's line. One note runs per room at a time. Nothing here reaches ESPN.

Dime in the room comes with the AI desk: with AI_ENABLED off, or for an account not
on AI_AGENT_USER_IDS, the room's view says so and every start is refused. The
draft-only tools are in no tool list but a draft conversation's. The account's own
switch (usr.ai_settings.draft_rooms, on by default) turns it off for one user: the
view reports `switched_off`, a start is refused with 409, and a seat Dime holds is
handed back before its next pick. With the account's memory off, a
real draft's plan and recap stay in the room: the briefs ask for no recall or
remember, and `set_draft_plan` saves nothing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from peewee import IntegrityError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.deps import OwnedDraftSessionContext, _owned_session
from core.errors import AppError, ConflictError
from core.logging import get_logger
from core.nba_calendar import nba_date_et
from core.settings import settings
from db.base import db, db_operation, run_db
from db.models.dime_draft import AiDraftNote, AiDraftRoom
from db.models.drafts import DraftPick, DraftSession
from schemas.draft import DraftPickCreate, DraftSessionUpdate, MockAdvanceRequest

PLAN, PICK, RECAP = "plan", "pick", "recap"
KINDS = (PLAN, PICK, RECAP)
RUNNING, DONE, FAILED = "running", "done", "failed"
SEAT_OFF, SEAT_DRAFTING, SEAT_STOPPING = "off", "drafting", "stopping"

# The room asks for Dime's pick once the user's pick is this many picks away (or nearer)
NOTE_LEAD = 2
# How many times Dime picks again for one pick after the player it chose was drafted first
RETAKES = 1
# Notes shown in the room, newest first
VIEW_NOTES = 40
# The AI desk's Draft notes view: the newest notes across the user's rooms, by room
DESK_NOTES = 300
DESK_ROOMS = 30
# A note still `running` this long past its turn's own deadline died with its process
_STALE_MARGIN = timedelta(seconds=60)
# A seat loop touches its row before every step; one this quiet died with its process
_SEAT_QUIET = timedelta(seconds=60)
# A 30-round, 20-team draft is 30 of the user's picks; the loop never runs past this many steps
_SEAT_STEPS = 200
# The seat's turn while another note is being written in the room: it looks again this often
# (seconds), and waits for at most this many notes before one pick
_BUSY_WAIT = 2.0
_BUSY_WAITS = 3
MAX_PLAN = 500
MAX_REASON = 300
MAX_PUNTS = 4

GET_DRAFT_RECAP_NAME = "get_draft_recap"
SET_DRAFT_PLAN_NAME = "set_draft_plan"
DRAFT_PICK_NAME = "draft_pick"
RECOMMEND_PICK_NAME = "recommend_pick"
DRAFT_ONLY_TOOL_NAMES = frozenset({SET_DRAFT_PLAN_NAME, DRAFT_PICK_NAME, RECOMMEND_PICK_NAME})
# Dime's one line on why its pick suits this roster: the board shows it under his row
MAX_PICK_REASON = 240


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    """The server has Dime at all (AI_ENABLED): the draft has no switch of its own."""
    return settings.ai_enabled


class DraftSwitchedOff(ConflictError):
    error_code = "AI_DRAFT_SWITCHED_OFF"
    default_message = "Dime in draft rooms is off for your account. Turn it on in Account, AI and automation"


async def features(user_id: int) -> Any:
    """The account's AI feature switches (usr.ai_settings, services/ai/ai_settings.py): `draft_rooms`
    and `memory`, both on for an account that never set them."""
    from services.ai import ai_settings
    return await ai_settings.get(user_id)


async def ensure_on(user_id: int) -> Any:
    """Refuse a start in the room unless the AI desk is on for the account (AI_ENABLED and its list)
    and the account's draft rooms switch allows Dime there. Returns the account's switches."""
    from services.ai.agent_service import ensure_access
    ensure_access(user_id)
    switches = await features(user_id)
    if not switches.draft_rooms:
        raise DraftSwitchedOff()
    return switches


# ------------------------------- the tools ------------------------------- #


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftRecapInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)


class SetDraftPlanInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)
    plan: str = Field(..., min_length=1, max_length=MAX_PLAN)
    punts: list[str] = Field(default_factory=list, max_length=MAX_PUNTS)

    @field_validator("plan")
    @classmethod
    def _plan(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("the plan is empty")
        return v


class DraftPickInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)
    player_id: int = Field(..., gt=0, lt=2**31)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON)


class RecommendPickInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)
    player_id: int = Field(..., gt=0, lt=2**31)
    reason: str = Field(..., min_length=1, max_length=MAX_PICK_REASON)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("the reason is empty")
        return v


GET_DRAFT_RECAP: dict[str, Any] = {
    "name": GET_DRAFT_RECAP_NAME,
    "description": (
        "One of the user's draft rooms graded after the fact (a finished draft, or one still running): each "
        "seat's grade (A-F, against the other seats in this room only), its place, its value over slot (what "
        "its picks were worth against the player the board ranked at each pick number), and how it drafted "
        "against ESPN's ADP: reach (how many picks ahead of ADP its players went, on average; negative means "
        "it waited) with its biggest reaches and steals. In a category league each seat's strongest and "
        "weakest categories as drafted; in a points league its projected season value and rank. Then the "
        "user's own picks with each one's ADP and value over slot, and which of them Dime made (by_dime). "
        "league_team_id matches get_league's. Who drafts by ADP and who reaches, and where a roster starts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"room_id": {"type": "integer", "description": "A room_id from get_draft_rooms"}},
        "required": ["room_id"],
        "additionalProperties": False,
    },
}

SET_DRAFT_PLAN: dict[str, Any] = {
    "name": SET_DRAFT_PLAN_NAME,
    "description": (
        "Only in a draft plan note: set the plan this room is drafted to. `plan` is the plan in a few "
        "lines (what to build, when to take which positions, who to target early). `punts` are the "
        "categories it concedes, as the board's category keys (at most 4; none in a points league, and "
        "an empty list clears them): they become the room's punts, which the board's recommendations "
        "score with. In a real draft with a team the plan is also saved to the team's notes. Call it "
        "again to change the plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "room_id": {"type": "integer", "description": "The room this note is for"},
            "plan": {"type": "string", "maxLength": MAX_PLAN, "description": "The plan, in a few lines"},
            "punts": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PUNTS,
                      "description": "Category keys to concede, from the board's categories"},
        },
        "required": ["room_id", "plan"],
        "additionalProperties": False,
    },
}

DRAFT_PICK: dict[str, Any] = {
    "name": DRAFT_PICK_NAME,
    "description": (
        "Only while you hold the user's seat in a mock room and are on the clock: draft this player "
        "(an NBA player_id still on the board, within the user's position caps) at the user's pick, "
        "with a one-line reason the user reads beside the board. Once per pick; nothing reaches ESPN."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "room_id": {"type": "integer", "description": "The mock room you are drafting in"},
            "player_id": {"type": "integer", "description": "The NBA player_id to take"},
            "reason": {"type": "string", "maxLength": MAX_REASON, "description": "Why him, in one line"},
        },
        "required": ["room_id", "player_id", "reason"],
        "additionalProperties": False,
    },
}

RECOMMEND_PICK: dict[str, Any] = {
    "name": RECOMMEND_PICK_NAME,
    "description": (
        "Only in your pick for a draft room (not while you hold the seat): the player you would take at the "
        "user's next pick (an NBA player_id still on the board, within the user's position caps), with one "
        "line on why for this user's roster that they read under his row on the board. Once per pick."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "room_id": {"type": "integer", "description": "The room this pick is for"},
            "player_id": {"type": "integer", "description": "The NBA player_id you would take"},
            "reason": {"type": "string", "maxLength": MAX_PICK_REASON,
                       "description": "Why him for this roster, in one line"},
        },
        "required": ["room_id", "player_id", "reason"],
        "additionalProperties": False,
    },
}

# What only a draft note's conversation offers, after everything else in its tool list (append only:
# recommend_pick came last, 2026-11-26)
DRAFT_ONLY_TOOLS: list[dict[str, Any]] = [SET_DRAFT_PLAN, DRAFT_PICK, RECOMMEND_PICK]


# ------------------------------- the rows ------------------------------- #


@db_operation("ai.draft_room")
def _room(session_id: int) -> Optional[AiDraftRoom]:
    return AiDraftRoom.get_or_none(AiDraftRoom.session_id == session_id)


@db_operation("ai.draft_room_set")
def _set_room(session_id: int, user_id: int, **fields: Any) -> AiDraftRoom:
    now = _now()
    (AiDraftRoom.insert(session_id=session_id, user_id=user_id, created_at=now, updated_at=now, **fields)
     .on_conflict(conflict_target=[AiDraftRoom.session_id], update={**fields, "updated_at": now})
     .execute())
    return AiDraftRoom.get(AiDraftRoom.session_id == session_id)


@db_operation("ai.draft_seat_take")
def _take_seat(session_id: int, user_id: int, quiet_before: datetime) -> bool:
    """Seat to drafting; True when this call should start the loop: the seat was off, or its loop
    went quiet (it died with its process). One already drafting, or stopping, keeps its loop."""
    now = _now()
    AiDraftRoom.insert(session_id=session_id, user_id=user_id, created_at=now, updated_at=now).on_conflict_ignore().execute()
    started = (AiDraftRoom.update(seat=SEAT_DRAFTING, seat_reason=None, updated_at=now)
               .where(AiDraftRoom.session_id == session_id,
                      (AiDraftRoom.seat == SEAT_OFF) | (AiDraftRoom.updated_at < quiet_before))
               .execute())
    if started:
        return True
    # Stopping, and asked back: the loop that is finishing its pick carries on
    (AiDraftRoom.update(seat=SEAT_DRAFTING, seat_reason=None)
     .where(AiDraftRoom.session_id == session_id, AiDraftRoom.seat == SEAT_STOPPING).execute())
    return False


@db_operation("ai.draft_seat_release")
def _release_seat(session_id: int, quiet_before: datetime) -> None:
    """The user asked for the seat back: a live loop stops after the pick it is making; one that went
    quiet is off now."""
    now = _now()
    (AiDraftRoom.update(seat=SEAT_OFF, seat_reason="stopped", updated_at=now)
     .where(AiDraftRoom.session_id == session_id, AiDraftRoom.seat != SEAT_OFF,
            AiDraftRoom.updated_at < quiet_before).execute())
    (AiDraftRoom.update(seat=SEAT_STOPPING)
     .where(AiDraftRoom.session_id == session_id, AiDraftRoom.seat == SEAT_DRAFTING).execute())


@db_operation("ai.draft_seat_touch")
def _touch_seat(session_id: int) -> Optional[str]:
    """The loop's heartbeat; returns the seat as it stands."""
    AiDraftRoom.update(updated_at=_now()).where(AiDraftRoom.session_id == session_id).execute()
    row = AiDraftRoom.get_or_none(AiDraftRoom.session_id == session_id)
    return row.seat if row is not None else None


@db_operation("ai.draft_seat_off")
def _seat_off(session_id: int, reason: str) -> None:
    (AiDraftRoom.update(seat=SEAT_OFF, seat_reason=reason, updated_at=_now())
     .where(AiDraftRoom.session_id == session_id).execute())


def _drop_undone_now(session_id: int) -> int:
    """A pick Dime made that was undone since (the user took the seat back and undid it, or a
    keystroke did): its seat note no longer stands for a pick, so it fails `undone`, leaving that
    pick free for the seat to make again and out of the room's list of Dime's picks."""
    kept = DraftPick.select(DraftPick.overall_pick).where(DraftPick.session == session_id, DraftPick.source == "dime")
    return (AiDraftNote.update(state=FAILED, reason="undone", finished_at=_now())
            .where(AiDraftNote.session_id == session_id, AiDraftNote.kind == PICK, AiDraftNote.seat == True,  # noqa: E712
                   AiDraftNote.state == DONE, AiDraftNote.for_pick.not_in(kept))
            .execute())


@db_operation("ai.draft_drop_undone")
def _drop_undone(session_id: int) -> int:
    return _drop_undone_now(session_id)


@db_operation("ai.draft_note_claim")
def _claim(session_id: int, user_id: int, kind: str, for_pick: Optional[int], seat: bool,
           facts: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Insert the note `running`: (its id, None), or (None, the note already written for this pick),
    or (None, None) when another note is running in the room."""
    if kind == PICK and seat:
        _drop_undone_now(session_id)
    if kind == PICK:
        held = (AiDraftNote.select(AiDraftNote.id, AiDraftNote.state, AiDraftNote.pick)
                .where(AiDraftNote.session_id == session_id, AiDraftNote.kind == PICK,
                       AiDraftNote.for_pick == for_pick, AiDraftNote.seat == seat, AiDraftNote.state != FAILED)
                .first())
        if held is not None and not (not seat and _retire_now(session_id, for_pick, held)):
            return None, held.id
    try:
        with db.atomic():
            note_id = AiDraftNote.insert(session_id=session_id, user_id=user_id, kind=kind, for_pick=for_pick,
                                         seat=seat, facts=facts, state=RUNNING, created_at=_now()).execute()
    except IntegrityError:
        # The pick's note landed between the read and the insert, or another note is running
        held = (AiDraftNote.select(AiDraftNote.id)
                .where(AiDraftNote.session_id == session_id, AiDraftNote.kind == PICK,
                       AiDraftNote.for_pick == for_pick, AiDraftNote.seat == seat, AiDraftNote.state != FAILED)
                .first()) if kind == PICK else None
        return None, held.id if held is not None else None
    return note_id, None


def _retire_now(session_id: int, for_pick: Optional[int], held: AiDraftNote) -> bool:
    """Whether the finished pick note held for `for_pick` gives way to a new one, retiring it (failed,
    which frees the pick): a note written before Dime chose players (no `pick`, from before 0058) as
    `superseded`, and Dime's pick whose player another seat drafted before the user picked as `taken`,
    unless Dime already picked again `RETAKES` times for this pick. A running note always holds."""
    if held.state != DONE:
        return False
    chosen = (held.pick or {}).get("player_id")
    if chosen is None:
        reason = "superseded"
    else:
        if not DraftPick.select().where(DraftPick.session == session_id, DraftPick.player == chosen).exists():
            return False
        retaken = (AiDraftNote.select().where(AiDraftNote.session_id == session_id, AiDraftNote.kind == PICK,
                                              AiDraftNote.for_pick == for_pick, AiDraftNote.seat == False,  # noqa: E712
                                              AiDraftNote.reason == "taken").count())
        if retaken >= RETAKES:
            return False
        reason = "taken"
    return bool(AiDraftNote.update(state=FAILED, reason=reason)
                .where(AiDraftNote.id == held.id, AiDraftNote.state == DONE).execute())


@db_operation("ai.draft_note_attach")
def _attach(note_id: int, conversation_id: int) -> None:
    AiDraftNote.update(conversation_id=conversation_id).where(AiDraftNote.id == note_id).execute()


@db_operation("ai.draft_note_settle")
def _settle(note_id: int, state: str, reason: Optional[str], text: Optional[str], turn_id: Optional[int],
            cost: int) -> None:
    (AiDraftNote.update(state=state, reason=reason, text=text, turn_id=turn_id, cost_micros=cost,
                        finished_at=_now())
     .where(AiDraftNote.id == note_id, AiDraftNote.state == RUNNING).execute())


@db_operation("ai.draft_note_sweep")
def _sweep(session_id: int, before: datetime) -> int:
    return (AiDraftNote.update(state=FAILED, reason="stale", finished_at=_now())
            .where(AiDraftNote.session_id == session_id, AiDraftNote.state == RUNNING,
                   AiDraftNote.created_at < before)
            .execute())


@db_operation("ai.draft_note_busy")
def _busy(session_id: int) -> bool:
    """A note is being written in the room now."""
    return (AiDraftNote.select(AiDraftNote.id)
            .where(AiDraftNote.session_id == session_id, AiDraftNote.state == RUNNING).exists())


@db_operation("ai.draft_note_get")
def _note(note_id: int) -> Optional[AiDraftNote]:
    return AiDraftNote.get_or_none(AiDraftNote.id == note_id)


@db_operation("ai.draft_note_update")
def _update_note(note_id: int, **fields: Any) -> None:
    AiDraftNote.update(**fields).where(AiDraftNote.id == note_id).execute()


@db_operation("ai.draft_notes")
def _notes(session_id: int, limit: int) -> list[AiDraftNote]:
    return list(AiDraftNote.select().where(AiDraftNote.session_id == session_id)
                .order_by(AiDraftNote.id.desc()).limit(limit))


@db_operation("ai.draft_plan_of")
def _plan_of(session_id: int) -> Optional[AiDraftNote]:
    """The room's newest plan: what pick notes and the seat draft to."""
    return (AiDraftNote.select()
            .where(AiDraftNote.session_id == session_id, AiDraftNote.kind == PLAN, AiDraftNote.plan.is_null(False))
            .order_by(AiDraftNote.id.desc()).first())


def _stale_before(now: datetime) -> datetime:
    return now - timedelta(seconds=settings.dime_draft_timeout_seconds) - _STALE_MARGIN


def _quiet_before(now: datetime) -> datetime:
    return now - timedelta(seconds=settings.dime_draft_timeout_seconds) - _SEAT_QUIET


def _seat_live(row: Optional[AiDraftRoom], notes: list[AiDraftNote], now: datetime) -> bool:
    """Dime holds the seat and its loop is still beating: a note being written, or a heartbeat inside
    the quiet window (a seat whose process died reads as stopped)."""
    if row is None or row.seat == SEAT_OFF:
        return False
    quiet = row.updated_at is not None and row.updated_at < _quiet_before(now)
    return any(n.state == RUNNING for n in notes) or not quiet


# ------------------------------- the room ------------------------------- #


def _status(value: Any) -> str:
    return getattr(value, "value", value)


def seatable(room: OwnedDraftSessionContext) -> Optional[str]:
    """Why Dime can't hold the user's seat in this room, or None when it can: a mock following
    nothing, a snake with a shape and the user's seat set, still running (the autopicker's rules)."""
    if room.kind != "mock":
        return "Dime takes your seat only in a mock room: a live ESPN draft is yours to pick in, with Dime's notes"
    if room.espn_league_id is not None:
        return "This mock follows an ESPN lobby, whose picks come from there"
    if room.status != "active":
        return "This draft is finished"
    if room.draft_type != "snake":
        return "Dime drafts snake rooms only"
    if not room.pick_order or not room.rounds:
        return "Set the room's pick order and rounds first"
    if room.my_slot is None:
        return "Set your seat first"
    return None


def following_pick(detail: Any) -> Optional[int]:
    """The user's turn after `my_next_pick`: whether a player lasts to it is what makes taking him now
    or later the question (the board's availability counts only to `my_next_pick`)."""
    from services.draft_service import next_pick_for_slot
    if detail.my_next_pick is None or detail.my_slot is None:
        return None
    keepers = [p.overall_pick for p in (detail.picks or []) if _status(p.source) == "keeper"]
    return next_pick_for_slot(detail.my_next_pick + 1, detail.my_slot, detail.league_size,
                              _status(detail.draft_type), skip=keepers, last=detail.total_picks)


def round_of_pick(pick: Optional[int], league_size: Optional[int]) -> Optional[int]:
    if pick is None or not league_size:
        return None
    return (pick - 1) // league_size + 1


# What an in-season board adds to a player, where known (league._live_of): carried only when present
LIVE_KEYS = ("move_7d", "move_1d", "rank_reason", "espn_rank_since", "espn_rank_since_or_earlier")


def facts_of(board: dict[str, Any]) -> dict[str, Any]:
    """The numbers a pick note read, off get_draft_board's result: the top three recommendations, who
    the market says won't last to the user's next pick, the punts, and the weakest categories."""
    on_clock = board.get("picks_until_my_turn") == 0
    top = [
        {**{k: r.get(k) for k in ("player_id", "name", "position", "team", "score", "availability", "injury",
                                  "adp", "cv_rank", "market_rank", "reason")},
         **{k: r[k] for k in LIVE_KEYS if k in r}}
        for r in (board.get("recommendations") or [])[:3]
    ]
    gone = [
        {"player_id": r.get("player_id"), "name": r.get("name"), "positions": r.get("positions"),
         "adp": r.get("adp")}
        for r in (board.get("best_available") or []) if r.get("availability") == "gone"
    ][:5]
    needs = sorted((n for n in (board.get("category_need") or []) if not n.get("punted")),
                   key=lambda n: -(n.get("need") or 0))[:3]
    return {
        "pick": board.get("my_next_pick"),
        "picks_until": board.get("picks_until_my_turn"),
        "on_clock": on_clock,
        # Who "gone" is about: the user's next pick, the one on the clock included (nobody, then)
        "gone_before": board.get("my_next_pick"),
        "top": top,
        "gone": gone,
        "punts": list(board.get("punts") or []),
        "roster": len(board.get("roster") or []),
        "needs": [{"category": n.get("category"), "label": n.get("label"), "need": n.get("need")} for n in needs],
    }


def _board_block(board: dict[str, Any]) -> str:
    """The board as the brief carries it: get_draft_board's result, trimmed to what a pick needs."""
    keep_rec = ("player_id", "name", "position", "team", "score", "vorp", "season_value", "availability", "injury",
                "adp", "cv_rank", "market_rank", "season_gp", "season_fpts_avg", "reason")
    out = {
        "picks_made": board.get("picks_made"),
        "my_next_pick": board.get("my_next_pick"),
        "picks_until_my_turn": board.get("picks_until_my_turn"),
        "punts": board.get("punts"),
        "recommendations": [
            {**{k: r.get(k) for k in keep_rec},
             **{k: r[k] for k in LIVE_KEYS if k in r},
             "components": [{"label": c.get("label"), "value": c.get("value"), "in_score": c.get("in_score")}
                            for c in (r.get("components") or [])]}
            for r in (board.get("recommendations") or [])
        ],
        "best_available": board.get("best_available") or [],
        "roster": board.get("roster") or [],
    }
    if board.get("category_need"):
        out["category_need"] = board["category_need"]
    if board.get("in_season"):
        out["in_season"] = board["in_season"]
    return json.dumps(out, default=str, ensure_ascii=False, separators=(",", ":"))


def _season_words(board: Optional[dict[str, Any]]) -> str:
    """For a draft held during the season, what the board's numbers already carry."""
    found = (board or {}).get("in_season")
    if not found:
        return ""
    if found.get("live_as_of"):
        return (
            "This draft is held during the season: the board values each player on Court Vision's live "
            f"projection of {found.get('live_as_of')} (his line from this season's games and the projection, "
            "his minutes for who is out on his team, and the games he is expected to play from "
            f"{found.get('games_from')} on), a game he misses costs the gap to a replacement, and its injury "
            "flags are today's. season_gp and season_fpts_avg are what he has done so far. ADP is the "
            "preseason crowd's." + _live_words(found) + " Weigh a hot or cold start by your playbook's draft "
            "rules.\n\n"
        )
    return (
        "This draft is held during the season: the board's values blend this season's games (season_gp, "
        f"season_fpts_avg) with the projection, count only the games from {found.get('games_from')} on, and "
        "its injury flags are today's. ADP is the preseason crowd's." + _live_words(found) + " Weigh a hot or "
        "cold start by your playbook's draft rules.\n\n"
    )


def _live_words(found: dict[str, Any]) -> str:
    """What the stored daily boards add to an in-season board, when they are there."""
    words = ""
    moves = found.get("moves")
    if moves:
        words += (
            f" move_7d and move_1d are the places a player gained on Court Vision's standard {moves.get('board')} "
            "board (negative is a fall), and rank_reason is what the projection read about him: say it when it "
            "explains a pick."
        )
    if found.get("espn_ranks_as_of"):
        words += (
            f" market_rank is ESPN's rank of {found.get('espn_ranks_as_of')}, and espn_rank_since the day ESPN "
            "last changed it: a rank ESPN has left alone through a player's rise or fall is a stale one."
        )
    return words


def _room_words(room: OwnedDraftSessionContext, detail: Any) -> str:
    kind = {"mock": "a mock draft", "live": "a live ESPN draft", "manual": "a draft entered by hand",
            "import": "an imported ESPN draft"}.get(room.kind, "a draft")
    fmt = _status(getattr(detail, "scoring_format", None)) or "unknown"
    bits = [kind, f"{fmt} scoring"]
    if detail.league_size:
        bits.append(f"{detail.league_size} teams")
    if detail.rounds:
        bits.append(f"{detail.rounds} rounds")
    if room.my_slot is not None:
        bits.append(f"the user's seat {room.my_slot}")
    team = f"team_id {room.team_id}" if room.team_id is not None else "no team (a room with generic settings)"
    return f"{', '.join(bits)}; {team}"


def _plan_line(plan: Optional[AiDraftNote]) -> str:
    if plan is None or not plan.plan:
        return "No plan is set for this room."
    punts = ", ".join(plan.punts or []) or "none"
    return f'The room\'s plan: "{plan.plan}" (punts: {punts}).'


def _real(room: OwnedDraftSessionContext) -> bool:
    """A real draft (not a mock) on one of the user's teams: what its notes may be saved to."""
    return room.kind != "mock" and room.team_id is not None


def _following_line(following: Optional[int]) -> str:
    if following is None:
        return "It is the user's last pick.\n\n"
    return (f"The user's pick after this one is #{following}: a player whose adp is well after #{following} "
            "will likely still be there then, so taking him now needs a reason.\n\n")


def brief(kind: str, room: OwnedDraftSessionContext, detail: Any, *, board: Optional[dict[str, Any]] = None,
          plan: Optional[AiDraftNote] = None, seat: bool = False, for_pick: Optional[int] = None,
          memory: bool = True, following: Optional[int] = None) -> str:
    """The note's message. With the account's memory off, it asks for no recall or remember (the
    tools would only answer that memory is off) and a real draft's plan stays in the room."""
    name = f' ("{detail.name}")' if getattr(detail, "name", None) else ""
    where = f"room_id {room.session_id}{name}: {_room_words(room, detail)}"
    rnd = round_of_pick(for_pick, detail.league_size)
    at = f"pick #{for_pick}" + (f" (round {rnd})" if rnd else "")
    if kind == PLAN:
        save = ("It is also saved to the team's notes as this draft's plan." if _real(room) and memory
                else "This room's plan stays in the room (memory is off for this account)." if _real(room)
                else "This room's plan stays in the room (a mock, or a room without a team).")
        recall = ("" if not memory
                  else f"- recall your notes on team_id {room.team_id} (the league, its managers, earlier drafts).\n"
                  if room.team_id is not None else "- recall your notes on earlier drafts.\n")
        return (
            f"[Draft plan for {where}. The user asked for it from the Draft desk.]\n\n"
            "Set the plan this draft is played to:\n"
            + recall
            + "- get_draft_board for the room: who the board values, ESPN's market, and in a category league "
            "where an average roster stands (category_need lists the category keys). In a draft held during "
            "the season (in_season on the board), note who is well ahead of or behind his projection where "
            "it changes the plan.\n"
            "- Decide the plan. In a category league: which categories to concede (none, or up to three that "
            "go together, like FT% with big men), and why; in a points league there are none to punt. Then when "
            "to take centers and guards against the roster slots, and who to target with the first picks "
            "(cite the board).\n"
            f"- set_draft_plan once, with the plan in a few lines and the punts. {save}\n\n"
            "End with the plan for the user, in three or four short lines beside the board."
        )
    if kind == PICK and seat:
        return (
            f"[You hold the user's seat in {where}. {at} is theirs, and you are on the clock. The user asked "
            "you to draft this mock for them; nothing here reaches ESPN.]\n\n"
            f"{_plan_line(plan)}\n\n"
            + _season_words(board) +
            "The room's board, read just now (get_draft_board's result; cite these numbers):\n"
            f"{_board_block(board or {})}\n\n"
            + _following_line(following) +
            "Make the pick:\n"
            "- Take who the plan and the board say. Going past the top recommendations needs a reason the board "
            "shows (a position run, a punt, a cap), and so does taking a player who will likely last.\n"
            "- get_player_status first only if the player you'd take has an injury listed.\n"
            "- draft_pick once, with his player_id and a one-line reason.\n\n"
            "End with one line for the user: who you took and why."
        )
    if kind == PICK:
        when = "now: the user is on the clock" if (board or {}).get("picks_until_my_turn") == 0 else \
            f"in {(board or {}).get('picks_until_my_turn')} picks"
        return (
            f"[Your pick for {where}. The user's {at} is {when}. The board highlights the player you choose, "
            "with your reason under his row; the user makes the pick.]\n\n"
            + _season_words(board) +
            "The room's board, read just now (get_draft_board's result; cite these numbers):\n"
            f"{_board_block(board or {})}\n\n"
            + _following_line(following) +
            "Choose one player for this pick:\n"
            "- Weigh the user's roster so far (roster, and category_need in a category league) against the "
            "recommendations: who fills what this team lacks, at a value the board backs. availability says "
            f"who won't last to pick #{for_pick}.\n"
            "- get_player_status only for an injured player you'd take.\n"
            "- recommend_pick once, with his player_id and one line on why for this roster (under 30 words, "
            "naming what the team has or lacks, not the board's numbers alone).\n\n"
            "End with that same line and nothing else."
        )
    # RECAP
    real = _real(room)
    steps = (
        "- Memory is off for this account, so nothing is saved: say how the user's seat did against the room "
        "and why, and if you made its picks (by_dime), what you'd do differently.\n"
        if not memory else
        f"- recall your notes on team_id {room.team_id}, then remember what the season starts from as one plan "
        "note on that team (where the roster is thin, which categories it should win, the picks to watch), with "
        "a recheck day two weeks into the season.\n"
        "- For each league-mate whose picks show a habit (several reaches at one position, drafting strictly by "
        "ADP, waiting on centers), remember it as an opponent note naming the league_team_id; only where several "
        "picks show it.\n"
        if real else
        "- This is a mock (or a room without a team): say how the user's seat did against the room and why. If "
        "you made its picks (by_dime), say what you'd do differently. remember a lesson only when recall shows "
        "an earlier draft pointing the same way, and name both.\n"
    )
    return (
        f"[Draft recap for {where}. The user asked for it from the Draft desk.]\n\n"
        "- get_draft_recap for the room: every seat's grade, its value over slot, how it drafted against ADP, "
        "and the user's picks.\n"
        + steps +
        "\nEnd with a short recap for the user beside the board: the grade and place, the best and worst pick, "
        "and what the roster needs first. A few lines."
    )


def title_of(kind: str, detail: Any, *, for_pick: Optional[int] = None, seat: bool = False) -> str:
    name = getattr(detail, "name", None) or {"mock": "Mock draft", "live": "Live draft"}.get(
        _status(getattr(detail, "kind", None)), "Draft")
    what = {PLAN: "plan", RECAP: "recap"}.get(kind) or (f"took pick {for_pick}" if seat else f"pick {for_pick}")
    return f"Dime · {name} · {what}"


# ------------------------------- views ------------------------------- #


def note_view(row: AiDraftNote) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "for_pick": row.for_pick,
        "seat": row.seat,
        "state": row.state,
        "reason": row.reason,
        "facts": row.facts or {},
        "text": row.text,
        "plan": row.plan,
        "punts": row.punts,
        "punts_before": row.punts_before,
        "took": row.took,
        "pick": row.pick,
        "conversation_id": row.conversation_id_id,
        "cost_usd": (row.cost_micros or 0) / 1e6,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


async def room_view(user_id: int, room: OwnedDraftSessionContext) -> dict[str, Any]:
    """What the Draft desk shows of Dime in a room. Never refuses: off, or for a user who isn't on the
    AI desk's list, it says so and the room hides Dime."""
    from services.ai.agent_service import has_access
    out: dict[str, Any] = {
        "enabled": enabled(), "available": False, "switched_off": False, "lead": NOTE_LEAD,
        "notes_on": False, "seat": {"state": SEAT_OFF, "reason": None, "live": False}, "can_seat": None,
        "running": None, "notes": [], "chat_id": None,
    }
    if not (out["enabled"] and has_access(user_id)):
        return out
    if not (await features(user_id)).draft_rooms:
        out["switched_off"] = True
        return out
    out["available"] = True
    now = _now()
    await _sweep(room.session_id, _stale_before(now))
    await _drop_undone(room.session_id)
    row = await _room(room.session_id)
    notes = await _notes(room.session_id, VIEW_NOTES)
    seat = row.seat if row is not None else SEAT_OFF
    running = next((n.id for n in notes if n.state == RUNNING), None)
    out.update({
        "notes_on": bool(row.notes) if row is not None else False,
        "seat": {"state": seat, "reason": row.seat_reason if row is not None else None,
                 "live": _seat_live(row, notes, now)},
        "can_seat": seatable(room) is None,
        "seat_blocker": seatable(room),
        "running": running,
        "notes": [note_view(n) for n in notes],
        "chat_id": row.chat_id_id if row is not None else None,
    })
    return out


@db_operation("ai.draft_notes_of_user")
def _desk_notes(user_id: int) -> tuple[list[AiDraftNote], dict[int, DraftSession]]:
    notes = list(AiDraftNote.select().where(AiDraftNote.user_id == user_id)
                 .order_by(AiDraftNote.id.desc()).limit(DESK_NOTES))
    ids = {n.session_id_id for n in notes}
    rooms = {r.id: r for r in DraftSession.select().where(DraftSession.id.in_(ids), DraftSession.user == user_id)} \
        if ids else {}
    return notes, rooms


async def desk_view(user_id: int) -> dict[str, Any]:
    """The AI desk's Draft notes: what Dime wrote in the user's draft rooms, by room, the room with the
    newest note first and each room's notes newest first. Listed whatever the switches say (the notes
    stay, as memory's do), with the server's and the account's switch beside them. A note whose turn
    died reads as failed, as its room would sweep it."""
    from services.ai.agent_service import ensure_access
    ensure_access(user_id)
    notes, rooms = await _desk_notes(user_id)
    stale = _stale_before(_now())
    out: list[dict[str, Any]] = []
    by_room: dict[int, dict[str, Any]] = {}
    for n in notes:
        room = rooms.get(n.session_id_id)
        if room is None:
            continue
        entry = by_room.get(room.id)
        if entry is None:
            if len(out) >= DESK_ROOMS:
                continue
            entry = by_room[room.id] = {
                "room_id": room.id, "name": room.name, "kind": room.kind, "status": room.status,
                "team_id": room.team_id, "last_at": n.created_at, "notes": [],
            }
            out.append(entry)
        view = note_view(n)
        if n.state == RUNNING and n.created_at is not None and n.created_at < stale:
            view.update(state=FAILED, reason="stale")
        entry["notes"].append(view)
    return {"enabled": enabled(), "switched_off": not (await features(user_id)).draft_rooms, "rooms": out}


async def set_notes(user_id: int, room: OwnedDraftSessionContext, on: bool) -> dict[str, Any]:
    await ensure_on(user_id)
    await _set_room(room.session_id, user_id, notes=on)
    return await room_view(user_id, room)


# ------------------------------- starting a note ------------------------------- #

# Notes and seat loops still running, held so they aren't collected
_RUNNING: set[asyncio.Task] = set()


def _keep(task: asyncio.Task) -> asyncio.Task:
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


@dataclass(frozen=True)
class Started:
    note_id: int
    task: Optional[asyncio.Task]     # None when the note was already there (a pick's)


async def _board(room: OwnedDraftSessionContext) -> dict[str, Any]:
    from services.ai import league
    return await league.get_draft_board(room, league.DraftBoardInput(room_id=room.session_id, available=12))


async def _detail(session_id: int) -> Any:
    from services.draft_service import DraftService
    return (await DraftService.get_session(session_id)).data


async def start(user_id: int, room: OwnedDraftSessionContext, kind: str, *, seat: bool = False) -> Started:
    """Start one note in the room: check the room can have it, read what it needs, claim its row,
    reserve its spend, open its conversation and start its turn. A pick note already written for the
    user's coming pick is returned as it is, with no new turn."""
    from services.ai import agent, conversations, spend

    switches = await ensure_on(user_id)
    if kind not in KINDS:
        raise AppError("AI_DRAFT_INVALID", "A note is a plan, a pick or a recap", status_code=422)
    now = _now()
    await _sweep(room.session_id, _stale_before(now))
    detail = await _detail(room.session_id)
    status = _status(detail.status)
    for_pick: Optional[int] = None
    board: Optional[dict[str, Any]] = None
    facts: dict[str, Any] = {}
    if kind in (PLAN, PICK) and status != "active":
        raise ConflictError("AI_DRAFT_FINISHED", "This draft is finished; ask for the recap instead")
    if kind == PICK:
        if detail.my_slot is None or detail.my_next_pick is None:
            raise ConflictError("AI_DRAFT_NO_PICK", "There is no pick of yours left to write a note for"
                                if detail.my_slot is not None else "Set your seat first")
        if seat and detail.picks_until_my_turn != 0:
            raise ConflictError("AI_DRAFT_NOT_YOUR_TURN", "Dime picks only when your seat is on the clock")
        for_pick = detail.my_next_pick
        board = await _board(room)
        facts = facts_of(board)
    elif kind == RECAP:
        if not detail.pick_count:
            raise ConflictError("AI_DRAFT_NO_PICKS", "Nothing has been drafted yet")
        facts = await _recap_facts(room)
    note_id, held = await _claim(room.session_id, user_id, kind, for_pick, seat, facts)
    if note_id is None:
        if held is not None:
            return Started(held, None)
        raise ConflictError("AI_DRAFT_BUSY", "Dime is already writing a note in this room; wait for it")
    if kind == PLAN:
        await _update_note(note_id, punts_before=list(room.punts))
    conversation_id: Optional[int] = None
    reservation_id: Optional[int] = None
    try:
        reservation_id = await spend.reserve(user_id)
        conversation = await conversations.create(user_id, room.team_id, kind=conversations.DRAFT,
                                                  title=title_of(kind, detail, for_pick=for_pick, seat=seat))
        conversation_id = conversation.id
        await conversations.claim(conversation_id, user_id,
                                  timedelta(seconds=settings.dime_draft_timeout_seconds + 30))
        await _attach(note_id, conversation_id)
        header_line = await agent.header(user_id, room.team_id, conversation_id=conversation_id)
        plan = await _plan_of(room.session_id) if kind == PICK and seat else None
        turn = agent.Turn(conversation_id=conversation_id, user_id=user_id,
                          message=brief(kind, room, detail, board=board, plan=plan, seat=seat, for_pick=for_pick,
                                        memory=switches.memory,
                                        following=following_pick(detail) if kind == PICK else None),
                          history=conversations.History(), reservation_id=reservation_id,
                          tools=agent.DRAFT_TOOLS, allowed=agent.DRAFT_ALLOWED, draft_note_id=note_id,
                          limits=agent.Limits.draft())
    except BaseException as exc:
        if reservation_id is not None:
            await spend.cancel(reservation_id)
        if conversation_id is not None:
            await conversations.release(conversation_id)
        code = exc.error_code if isinstance(exc, AppError) else "start_failed"
        await _settle(note_id, FAILED, code, None, None, 0)
        if not isinstance(exc, AppError):
            get_logger().exception("ai_draft_note_start_failed", note_id=note_id, session_id=room.session_id)
        raise
    task = _keep(asyncio.create_task(_run(turn, header_line, note_id, room.session_id, kind, seat)))
    return Started(note_id, task)


async def _run(turn: Any, header_line: str, note_id: int, session_id: int, kind: str, seat: bool) -> None:
    from services.ai import agent
    result = None
    try:
        result = await agent.run_turn(turn, agent.Emitter(), header_line=header_line)
    finally:
        ok = result is not None and result.outcome == "ok"
        text = turn.answer_text().strip() or None
        state, reason = (DONE, None) if ok else (FAILED, result.outcome if result is not None else "crashed")
        if ok and kind == PICK:
            # The seat's turn ends in a pick it made, and the room's in the one it chose
            note = await _note(note_id)
            if note is None or not (note.took if seat else note.pick):
                state, reason = FAILED, "no_pick"
        cost = result.cost if result is not None else turn.cost
        try:
            await _settle(note_id, state, reason, text, result.turn_id if result is not None else None, cost)
        except Exception:
            get_logger().exception("ai_draft_note_settle_failed", note_id=note_id)
        get_logger().info("ai_draft_note", note_id=note_id, session_id=session_id, kind=kind, seat=seat,
                          state=state, reason=reason, cost_micros=cost)


# ------------------------------- the room's chat ------------------------------- #

# The room's Dime chat (James, 2026-11-26, the Draft lab's Dime rework, step 2): the AI desk's agent
# and tools in a conversation of kind `room`, one per room (usr.ai_draft_rooms.chat_id, migration
# 0059), with the room's state read again at every message (`chat_line`). The messages are the
# user's own, so each one spends on the AI desk's line, as a chat there does. Nothing in it drafts:
# the draft-only tools are in no list of a chat's.


def chat_title(detail: Any) -> str:
    name = getattr(detail, "name", None) or {"mock": "Mock draft", "live": "Live draft"}.get(
        _status(getattr(detail, "kind", None)), "Draft")
    return f"Dime · {name} · chat"


@db_operation("ai.draft_chat_set")
def _set_chat(session_id: int, user_id: int, conversation_id: int,
              replace: Optional[int]) -> tuple[bool, Optional[int]]:
    """Point the room at `conversation_id`, over no chat or over `replace` (the chat the user started
    over from) only, so two first messages at once keep one chat. Returns whether this call put it
    there (from the update itself: a later read could already show a newer chat), and the room's chat.
    `updated_at` stays as it was: the seat's loop reads it as its heartbeat."""
    now = _now()
    AiDraftRoom.insert(session_id=session_id, user_id=user_id, created_at=now,
                       updated_at=now).on_conflict_ignore().execute()
    free = AiDraftRoom.chat_id.is_null()
    if replace is not None:
        free = free | (AiDraftRoom.chat_id == replace)
    if AiDraftRoom.update(chat_id=conversation_id).where(AiDraftRoom.session_id == session_id, free).execute():
        return True, conversation_id
    return False, AiDraftRoom.get(AiDraftRoom.session_id == session_id).chat_id_id


async def room_chat(user_id: int, room: OwnedDraftSessionContext, *, fresh: bool = False) -> int:
    """The room's Dime chat, started now when the room has none, or with `fresh` a new one in place of
    the one it has (a full chat starts over; the old one is kept, unlisted). Its conversation id."""
    from services.ai import conversations
    await ensure_on(user_id)
    row = await _room(room.session_id)
    current = row.chat_id_id if row is not None else None
    if current is not None and not fresh:
        return current
    detail = await _detail(room.session_id)
    made = await conversations.create(user_id, room.team_id, kind=conversations.ROOM, title=chat_title(detail))
    installed, kept = await _set_chat(room.session_id, user_id, made.id, current)
    if not installed:
        # Another first message started the room's chat meanwhile: one chat per room. A chat this call
        # did put there is never deleted, even once a later "start over" has replaced it (kept, unlisted)
        await conversations.delete(made.id, user_id)
    return kept


@db_operation("ai.draft_chat_room")
def _room_of_chat(conversation_id: int, user_id: int) -> Optional[int]:
    row = AiDraftRoom.get_or_none(AiDraftRoom.chat_id == conversation_id, AiDraftRoom.user_id == user_id)
    return row.session_id_id if row is not None else None


def _next_pick_words(detail: Any) -> str:
    status = _status(detail.status)
    if status == "completed":
        return "The draft is finished: get_draft_recap reads how every seat did."
    if status != "active":
        return f"The room is {status}."
    made = f"{detail.pick_count} pick{'' if detail.pick_count == 1 else 's'} made"
    if detail.my_next_pick is None:
        return f"{made}; " + ("the user's seat isn't set." if detail.my_slot is None else "the user has no pick left.")
    rnd = round_of_pick(detail.my_next_pick, detail.league_size)
    until = detail.picks_until_my_turn
    when = ("on the clock now" if until == 0 else f"in {until} pick{'' if until == 1 else 's'}"
            if until is not None else "")
    at = f"#{detail.my_next_pick}" + (f" (round {rnd})" if rnd else "")
    return f"{made}; the user's next pick is {at}" + (f", {when}." if when else ".")


def _dime_pick_words(notes: list[AiDraftNote], detail: Any) -> str:
    """Dime's pick for the user's next pick as the board shows it, or that one is being chosen."""
    if _status(detail.status) != "active" or detail.my_next_pick is None:
        return ""
    mine = [n for n in notes if n.kind == PICK and not n.seat and n.for_pick == detail.my_next_pick]
    if not mine:
        return ""
    note = mine[0]
    if note.state == RUNNING:
        return f"You are choosing the user's pick for #{detail.my_next_pick} now, in a note of its own."
    pick = note.pick or {}
    drafted = {p.player_id for p in detail.picks if p.player_id is not None}
    if note.state != DONE or pick.get("player_id") is None or pick["player_id"] in drafted:
        return ""
    reason = f': "{pick["reason"]}"' if pick.get("reason") else ""
    return (f"Your pick for #{detail.my_next_pick}, highlighted on the board: {pick.get('name')} "
            f"(player_id {pick['player_id']}){reason}.")


async def chat_line(user_id: int, conversation_id: int) -> str:
    """What a message in a room's chat knows of the room, read as it is sent (it follows the AI
    desk's header): which room, the user's next pick, who they have, the room's punts, Dime's pick
    on the board and whether Dime holds the seat. The board itself is a lookup (get_draft_board)."""
    session_id = await _room_of_chat(conversation_id, user_id)
    room = await run_db("ai.owned_session", _owned_session, session_id, user_id) if session_id is not None else None
    if room is None:
        return "[This chat's draft room is gone (deleted). Answer from the conversation so far.]"
    detail = await _detail(room.session_id)
    name = f' ("{detail.name}")' if getattr(detail, "name", None) else ""
    parts = [f"This is the Dime chat of draft room_id {room.session_id}{name}: {_room_words(room, detail)}.",
             _next_pick_words(detail)]
    mine = sorted((p for p in detail.picks if p.by_me), key=lambda p: p.overall_pick)
    if mine:
        parts.append("The user's picks so far: "
                     + ", ".join(f"#{p.overall_pick} {p.player_name or 'unknown'}" for p in mine) + ".")
    elif _status(detail.status) == "active":
        parts.append("The user hasn't drafted anyone yet.")
    if room.punts:
        parts.append(f"The room's punts, which the user set: {', '.join(room.punts)}.")
    # A note whose process died reads as running until swept, as the room's view sweeps it: it would
    # say Dime is choosing a pick, and keep a dead seat looking live
    now = _now()
    await _sweep(room.session_id, _stale_before(now))
    notes = await _notes(room.session_id, VIEW_NOTES)
    parts.append(_dime_pick_words(notes, detail))
    if _seat_live(await _room(room.session_id), notes, now):
        parts.append("You hold the user's seat in this mock and draft it pick by pick, until the user takes it back.")
    parts.append(f"The user is writing from the room, beside its board: read it with get_draft_board (room_id "
                 f"{room.session_id}) before naming players. The user makes the picks and sets the punts, so say "
                 "what you would do and leave both to them.")
    return "[" + " ".join(p for p in parts if p) + "]"


# ------------------------------- the seat ------------------------------- #


async def set_seat(user_id: int, room: OwnedDraftSessionContext, on: bool) -> dict[str, Any]:
    """Hand Dime the user's seat in a mock (it drafts to the end, pick by pick), or take it back (after
    the pick it is making)."""
    if on:
        await ensure_on(user_id)
    now = _now()
    if on:
        blocker = seatable(room)
        if blocker is not None:
            raise ConflictError("AI_DRAFT_NO_SEAT", blocker)
        if await _take_seat(room.session_id, user_id, _quiet_before(now)):
            _keep(asyncio.create_task(seat_loop(user_id, room.session_id)))
    else:
        await _release_seat(room.session_id, _quiet_before(now))
    return await room_view(user_id, room)


async def _wait_for_note(session_id: int) -> None:
    """The seat's turn came while another note is being written in the room (one the user asked for;
    a room has one note at a time): wait until it settles, the seat is taken back, or as long as a
    note may run before `start` sweeps it. The seat's row is touched at each look, so the loop
    stays live while it waits."""
    waited = 0.0
    limit = settings.dime_draft_timeout_seconds + _STALE_MARGIN.total_seconds()
    while waited < limit:
        await asyncio.sleep(_BUSY_WAIT)
        waited += _BUSY_WAIT
        if await _touch_seat(session_id) != SEAT_DRAFTING or not await _busy(session_id):
            return


async def seat_loop(user_id: int, session_id: int) -> str:
    """Draft the user's seat to the end: the autopicker runs the other seats up to the user's turn,
    Dime makes the pick, and again, until the draft ends, the user takes the seat back, or a step
    fails. Returns why it stopped (also stored as the seat's reason)."""
    from services.draft_mock_service import DraftMockService

    reason = "failed"
    steps = waits = 0
    try:
        while steps < _SEAT_STEPS:
            seat = await _touch_seat(session_id)
            if seat != SEAT_DRAFTING:
                reason = "stopped"
                return reason
            if not (await features(user_id)).draft_rooms:
                # The account switched Dime in draft rooms off: hand the seat back before the next pick
                reason = "switched_off"
                return reason
            room = await run_db("ai.draft_owned_session", _owned_session, session_id, user_id)
            if room is None:
                reason = "gone"
                return reason
            blocker = seatable(room)
            if blocker is not None:
                reason = "done" if room.status != "active" else "blocked"
                return reason
            detail = await _detail(session_id)
            if detail.my_next_pick is None:
                # None of the user's picks left: the other seats finish the draft
                await DraftMockService.advance(session_id, MockAdvanceRequest(until="end"))
                reason = "done"
                return reason
            if detail.picks_until_my_turn != 0:
                steps += 1
                ran = await DraftMockService.advance(session_id, MockAdvanceRequest(until="my_turn"))
                stopped = ran.data.stopped_reason if ran.data is not None else None
                if stopped in ("cap_blocked", "pool_exhausted"):
                    reason = stopped
                    return reason
                continue
            try:
                started = await start(user_id, room, PICK, seat=True)
            except ConflictError as exc:
                # A note the user asked for is still being written: the pick waits for it rather
                # than hand the seat back. The wait isn't a step, and every check above runs again
                # before the pick (the seat taken back, the switch, the room)
                if exc.error_code != "AI_DRAFT_BUSY" or waits >= _BUSY_WAITS:
                    raise
                waits += 1
                await _wait_for_note(session_id)
                continue
            steps, waits = steps + 1, 0
            if started.task is not None:
                await started.task
            note = await _note(started.note_id)
            if note is None or note.state != DONE or not note.took:
                reason = "pick_failed"
                return reason
        reason = "too_long"
        return reason
    except AppError as exc:
        reason = exc.error_code.lower()
        get_logger().warning("ai_draft_seat_stopped", session_id=session_id, reason=reason, message=exc.message)
        return reason
    except Exception:
        get_logger().exception("ai_draft_seat_failed", session_id=session_id)
        return reason
    finally:
        try:
            await _seat_off(session_id, reason)
        except Exception:
            get_logger().exception("ai_draft_seat_off_failed", session_id=session_id)


# ------------------------------- the tools' handlers ------------------------------- #


async def _owned_room(user_id: int, room_id: int) -> OwnedDraftSessionContext:
    room = await run_db("ai.owned_session", _owned_session, room_id, user_id)
    if room is None:
        raise AppError("DRAFT_NOT_FOUND", f"Draft room {room_id} is not one of the user's rooms")
    return room


async def _running_note(note_id: Optional[int], room_id: int, kind: str, what: str) -> AiDraftNote:
    note = await _note(note_id) if note_id is not None else None
    if note is None or note.kind != kind or note.state != RUNNING:
        raise AppError("AI_DRAFT_TOOL_REFUSED", f"{what} only works in Dime's {kind} note from the Draft desk")
    if note.session_id_id != room_id:
        raise AppError("AI_DRAFT_TOOL_REFUSED", f"This note is for room {note.session_id_id}, not {room_id}")
    return note


async def set_draft_plan(user_id: int, conversation_id: Optional[int], note_id: Optional[int],
                         args: SetDraftPlanInput) -> dict[str, Any]:
    from services.ai import memory
    from services.draft_service import DraftService

    note = await _running_note(note_id, args.room_id, PLAN, "set_draft_plan")
    room = await _owned_room(user_id, args.room_id)
    punts = list(dict.fromkeys(p.strip() for p in args.punts if p.strip()))
    if punts or room.punts:
        # Checked against the league's categories by the room's own update (a points room has none)
        updated = (await DraftService.update_session(room.session_id, DraftSessionUpdate(punts=punts))).data
        punts = list(updated.punts) if updated is not None else punts
    await _update_note(note.id, plan=args.plan, punts=punts)
    out: dict[str, Any] = {"room_id": room.session_id, "plan": args.plan, "punts": punts,
                           "punts_before": list(note.punts_before or [])}
    if _real(room) and (await features(user_id)).memory:
        try:
            saved = await memory.remember(
                memory.Caller(user_id=user_id, conversation_id=conversation_id, today=nba_date_et()),
                memory.RememberInput(kind="plan", team_id=room.team_id, text=f"Draft plan: {args.plan}"[:500]))
            if saved.get("note_id") is not None:
                out["note_id"] = saved["note_id"]
        except AppError as exc:
            out["note"] = f"Not saved to the team's notes: {exc.message}"
    return out


async def draft_pick(user_id: int, note_id: Optional[int], args: DraftPickInput) -> dict[str, Any]:
    from services.draft_board_service import BoardSession, DraftBoardService
    from services.draft_service import DraftService
    from services.scoring.resolver import resolve_scoring_for_room

    note = await _running_note(note_id, args.room_id, PICK, "draft_pick")
    if not note.seat:
        raise AppError("AI_DRAFT_TOOL_REFUSED", "draft_pick only works while you hold the user's seat in a mock")
    if note.took:
        raise AppError("AI_DRAFT_ALREADY_PICKED", f"You already took {note.took.get('name')} at this pick")
    room = await _owned_room(user_id, args.room_id)
    blocker = seatable(room)
    if blocker is not None:
        raise AppError("AI_DRAFT_TOOL_REFUSED", blocker)
    detail = await _detail(room.session_id)
    if detail.picks_until_my_turn != 0 or detail.my_next_pick != note.for_pick:
        raise AppError("AI_DRAFT_NOT_YOUR_TURN", f"Pick #{note.for_pick} isn't on the clock any more")
    scoring = resolve_scoring_for_room(room.league, room.scoring_format)
    board = await DraftBoardService.get_board(scoring, session=BoardSession.of(room))
    row = next((r for r in (board.data or []) if r.player_id == args.player_id), None)
    if row is None:
        raise AppError("AI_DRAFT_NOT_ON_BOARD", f"Player {args.player_id} isn't on the board: drafted already, "
                                                "or not draftable")
    if row.cap_blocked:
        raise AppError("AI_DRAFT_CAP_BLOCKED", f"{row.name} would break the user's {row.primary_position} cap")
    reason = " ".join(args.reason.split())[:MAX_REASON]
    made = (await DraftService.add_pick(
        room.session_id,
        DraftPickCreate(player_id=args.player_id, by_me=True, overall_pick=note.for_pick),
        server_source="dime",
    )).data
    took = {"overall_pick": made.overall_pick, "round": made.round, "player_id": args.player_id,
            "name": made.player_name or row.name, "position": row.primary_position, "team": row.team,
            "reason": reason}
    await _update_note(note.id, took=took)
    return {"room_id": room.session_id, "pick": made.overall_pick, "round": made.round,
            "player_id": args.player_id, "name": took["name"]}


async def recommend_pick(user_id: int, note_id: Optional[int], args: RecommendPickInput) -> dict[str, Any]:
    """Record Dime's choice for the user's next pick on its pick note: the room's board highlights him
    with the reason. Nothing is drafted."""
    from services.draft_board_service import BoardSession, DraftBoardService
    from services.scoring.resolver import resolve_scoring_for_room

    note = await _running_note(note_id, args.room_id, PICK, "recommend_pick")
    if note.seat:
        raise AppError("AI_DRAFT_TOOL_REFUSED", "You hold the seat in this room: make the pick with draft_pick")
    if note.pick:
        raise AppError("AI_DRAFT_ALREADY_PICKED", f"You already chose {note.pick.get('name')} for this pick")
    room = await _owned_room(user_id, args.room_id)
    scoring = resolve_scoring_for_room(room.league, room.scoring_format)
    board = await DraftBoardService.get_board(scoring, session=BoardSession.of(room))
    row = next((r for r in (board.data or []) if r.player_id == args.player_id), None)
    if row is None:
        raise AppError("AI_DRAFT_NOT_ON_BOARD", f"Player {args.player_id} isn't on the board: drafted already, "
                                                "or not draftable")
    if row.cap_blocked:
        raise AppError("AI_DRAFT_CAP_BLOCKED", f"{row.name} would break the user's {row.primary_position} cap")
    pick = {"player_id": args.player_id, "name": row.name, "position": row.primary_position, "team": row.team,
            "reason": args.reason}
    await _update_note(note.id, pick=pick)
    return {"room_id": room.session_id, "for_pick": note.for_pick, "player_id": args.player_id, "name": row.name}


# ------------------------------- get_draft_recap ------------------------------- #

# A pick taken this many picks ahead of (or after) ESPN's ADP is a reach (or a steal)
REACH_PICKS = 10


def _r(value: Optional[float], places: int = 1) -> Optional[float]:
    return round(value, places) if value is not None else None


def recap_of(recap: Any, *, league_team_ids: tuple[int, ...] = ()) -> dict[str, Any]:
    """get_draft_recap's result from the room's recap (services/draft_recap_service.py): pure."""
    meta = recap.meta
    picks = list(recap.data or [])
    by_slot: dict[int, list[Any]] = {}
    for p in picks:
        if p.slot is not None:
            by_slot.setdefault(p.slot, []).append(p)
    standings = {s.slot: s for s in (recap.standings or [])}

    def named(p: Any) -> dict[str, Any]:
        return {"pick": p.overall_pick, "name": p.player_name, "adp": _r(p.adp)}

    seats = []
    for seat in recap.seats or []:
        mine = by_slot.get(seat.slot, [])
        # How many picks ahead of ESPN's ADP each pick was made (negative: after it), from the pick
        # and the ADP themselves
        ahead = [(p, p.adp - p.overall_pick) for p in mine if p.adp is not None]
        gaps = [gap for _, gap in ahead]
        reaches = [p for p, gap in sorted(ahead, key=lambda x: -x[1]) if gap >= REACH_PICKS]
        steals = [p for p, gap in sorted(ahead, key=lambda x: x[1]) if gap <= -REACH_PICKS]
        entry: dict[str, Any] = {
            "slot": seat.slot,
            "league_team_id": seat.espn_team_id if seat.espn_team_id is not None else (
                league_team_ids[seat.slot - 1] if 0 < seat.slot <= len(league_team_ids) else None),
            "mine": seat.is_me,
            "picks": seat.picks,
            "grade": seat.grade,
            "place": seat.position,
            "value_over_slot": _r(seat.value_over_slot),
            # Positive: its players went that many picks ahead of ESPN's ADP on average
            "reach": _r(sum(gaps) / len(gaps)) if gaps else None,
            "reaches": [named(p) for p in reaches[:2]],
            "steals": [named(p) for p in steals[:2]],
        }
        line = standings.get(seat.slot)
        if line is not None and line.categories:
            ranked = sorted(line.categories, key=lambda c: c.rank)
            entry["strongest"] = [c.label for c in ranked[:2]]
            entry["weakest"] = [c.label for c in ranked[-2:]]
            entry["roto_rank"] = _r(line.roto_rank)
        elif line is not None and line.season_value is not None:
            entry["season_value"] = _r(line.season_value, 0)
            entry["value_rank"] = _r(line.value_rank)
        seats.append(entry)
    out: dict[str, Any] = {
        "format": meta.format if meta else None,
        "complete": meta.complete if meta else None,
        "picks_made": meta.picks_made if meta else len(picks),
        "my_slot": meta.my_slot if meta else None,
        "graded_by": meta.graded_by if meta else None,
        "seats": seats,
        "my_picks": [
            {"pick": p.overall_pick, "round": p.round, "player_id": p.player_id, "name": p.player_name,
             "adp": _r(p.adp), "value_over_slot": _r(p.value_over_slot), "by_dime": _status(p.source) == "dime"}
            for p in picks if p.by_me
        ],
    }
    if not seats:
        out["note"] = recap.message or "No seat could be graded yet"
    return out


async def _recap_of_room(room: OwnedDraftSessionContext) -> dict[str, Any]:
    from services.draft_recap_service import DraftRecapService
    from services.scoring.resolver import resolve_scoring_for_room
    scoring = resolve_scoring_for_room(room.league, room.scoring_format)
    recap = await DraftRecapService.get_recap(scoring, room)
    return {"room_id": room.session_id, **recap_of(recap, league_team_ids=room.pick_order)}


async def get_draft_recap(user_id: int, args: DraftRecapInput) -> dict[str, Any]:
    return await _recap_of_room(await _owned_room(user_id, args.room_id))


async def _recap_facts(room: OwnedDraftSessionContext) -> dict[str, Any]:
    """What a recap note shows beside Dime's words: the user's seat's grade and place."""
    try:
        found = await _recap_of_room(room)
    except Exception:
        get_logger().warning("ai_draft_recap_facts_failed", session_id=room.session_id)
        return {}
    mine = next((s for s in found["seats"] if s.get("mine")), None)
    return {"complete": found.get("complete"), "seats": len(found["seats"]),
            "grade": mine.get("grade") if mine else None, "place": mine.get("place") if mine else None,
            "value_over_slot": mine.get("value_over_slot") if mine else None,
            "by_dime": sum(1 for p in found["my_picks"] if p["by_dime"])}
