"""
What the user asks Dime to do at a time (usr.ai_asks, migration 0055; the Dime desk
redesign, step 2).

In a chat the user can ask Dime for something later: "at 6:30 tonight, check whether he
plays and swap him out if he doesn't", "Saturday morning, find me a stream for Sunday".
Dime saves it with `schedule_ask`, in the user's words and complete enough to act on alone,
and drops one with `cancel_ask`; the desk lists them (the timeline's "You asked" rows, the
chat's Scheduled block) and the user can cancel one there too. Only the user's own turns
save or cancel one: a check-in can't, so an ask never schedules another.

When an ask falls due it runs as a check-in of kind `ask` (services/ai/checkins.py): a
conversation of its own, titled with the ask, the check-in tools and bounds, the user's words
in its brief, and **one Dime turn on the check-ins budget** (the autopilot line, services/ai/
spend.py), reserved as it starts; a budget that refuses it leaves the ask skipped. Unlike a
scheduled check-in its proposals are live chips: a change on an automation at AI on the team
is sent when log_decision commits it, as in any check-in (services/ai/acting.py), and anything
else waits in its conversation as a chip the user sends. It runs on a team whose check-ins are
off too (then nothing is at AI, so every change is a chip).

The clock is in this process: `keep_running` looks for due asks every minute, so an ask
starts within a minute of its time. One that finds a check-in already running on its team
waits for it (one at a time per team, as every check-in), and one still waiting an hour past
its time is missed. A restart loses nothing: the next look finds what fell due meanwhile.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from peewee import JOIN
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.errors import AppError, ConflictError, NotFoundError
from core.logging import get_logger
from core.nba_calendar import EASTERN
from core.settings import settings
from db.base import db_operation
from db.models.autopilot import AiAsk, AiCheckin

log = get_logger("ai.asks")

SCHEDULE_ASK_NAME, CANCEL_ASK_NAME = "schedule_ask", "cancel_ask"
# The chat's block for an ask, under this key in the tool's result (agent._run_one)
ASK_KEY = "ask_block"

# The ask's own states; once started, how it went is its check-in's (`state_of`)
PENDING, STARTED, CANCELLED, MISSED, SKIPPED, FAILED = "pending", "started", "cancelled", "missed", "skipped", "failed"
# The states the desk shows: the ask's own, and its check-in's once it ran
RUNNING, DONE = "running", "done"

WHAT_MAX = 500
# Asks a team may have waiting at once, and how far ahead one may be
PER_TEAM = 10
AHEAD = timedelta(days=14)
# An ask still waiting this long past its time (the team was busy, the server was down) is missed
GRACE = timedelta(hours=1)
# How often this process looks for due asks
EVERY_SECONDS = 60
# Due asks started in one look, oldest first: the next look takes the rest
BATCH = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def et_words(at: datetime) -> str:
    """'Sun Nov 22, 6:30 PM': an instant as the user reads it, in Eastern (the brief and the tool)."""
    et = _aware(at).astimezone(EASTERN)
    return f"{et.strftime('%a %b')} {et.day}, {et.strftime('%I:%M %p').lstrip('0')}"


# ------------------------------- the tools ------------------------------- #


class ScheduleAskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)
    at: datetime
    what: str = Field(..., min_length=3, max_length=WHAT_MAX)

    @field_validator("what")
    @classmethod
    def _clean(cls, value: str) -> str:
        text = " ".join(value.replace("\x00", "").split())
        if len(text) < 3:
            raise ValueError("write what to do")
        return text

    @field_validator("at")
    @classmethod
    def _eastern(cls, value: datetime) -> datetime:
        # A wall time without an offset is Eastern, the clock the prompt and the tools speak (EASTERN is a
        # pytz zone: localize, never replace, or the offset is its 1883 one)
        return EASTERN.localize(value) if value.tzinfo is None else value


class CancelAskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)
    ask_id: int = Field(..., gt=0)


SCHEDULE_ASK: dict[str, Any] = {
    "name": SCHEDULE_ASK_NAME,
    "description": (
        "Save something the user asks you to do later, at a time they name (today or another day, up to two "
        "weeks ahead). At that time you run it as a check-in of its own, with their words as its brief, and "
        "until then the user sees it on the AI desk. Use it only when the user asks for something at a time "
        "(\"at 6:30 tonight check whether he plays\", \"Saturday morning find me a stream for Sunday\"); what "
        "they want now, you do now. at: when, in Eastern time (2026-11-22T18:30, or with an offset). what: what "
        "to do, complete enough to act on alone, since you won't have this chat then: name the team's players, "
        "the condition and what to do either way. Each ask runs one of your turns on the check-ins budget. When "
        "it runs, a change on an automation at AI on the team is sent as in a check-in, and anything else is "
        "a chip the user sends. Tell the user the time back in Eastern. A team holds 10 asks waiting at most. "
        "Only in the user's own turn, never in a check-in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "The team it is about: one of the user's team_ids"},
            "at": {"type": "string",
                   "description": "When to do it, Eastern: 2026-11-22T18:30 (or 2026-11-22T18:30:00-05:00)"},
            "what": {"type": "string", "minLength": 3, "maxLength": WHAT_MAX,
                     "description": "What to do, in a sentence or two, complete on its own"},
        },
        "required": ["team_id", "at", "what"],
        "additionalProperties": False,
    },
}

CANCEL_ASK: dict[str, Any] = {
    "name": CANCEL_ASK_NAME,
    "description": (
        "Cancel one of the user's asks that hasn't run yet, when they ask you to. get_automations lists the "
        "team's waiting asks under asks, with their ask_id. Only in the user's own turn, never in a check-in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "The ask's team: one of the user's team_ids"},
            "ask_id": {"type": "integer", "description": "From schedule_ask's result or get_automations' asks"},
        },
        "required": ["team_id", "ask_id"],
        "additionalProperties": False,
    },
}

# Appended after the steering tools in the AI desk's and a check-in conversation's lists (agent.py), never
# reordered: the lists are a cached prefix
ASK_TOOLS: list[dict[str, Any]] = [SCHEDULE_ASK, CANCEL_ASK]
ASK_TOOL_NAMES = frozenset(t["name"] for t in ASK_TOOLS)


# ------------------------------- the rows ------------------------------- #


def _joined():
    return (AiAsk.select(AiAsk, AiCheckin)
            .join(AiCheckin, JOIN.LEFT_OUTER, on=(AiAsk.checkin_id == AiCheckin.id), attr="ci"))


def _checkin_of(row: AiAsk) -> Optional[AiCheckin]:
    ci = getattr(row, "ci", None)
    return ci if ci is not None and getattr(ci, "id", None) is not None else None


@db_operation("ai.ask_insert")
def _insert(user_id: int, team_id: int, conversation_id: Optional[int], what: str, due_at: datetime,
            now: datetime) -> AiAsk:
    return AiAsk.create(user_id=user_id, team_id=team_id, conversation_id=conversation_id, what=what, due_at=due_at,
                        state=PENDING, created_at=now, updated_at=now)


@db_operation("ai.asks_waiting")
def _waiting(team_id: int) -> int:
    return AiAsk.select().where(AiAsk.team_id == team_id, AiAsk.state == PENDING).count()


@db_operation("ai.ask_get")
def _get(ask_id: int, user_id: Optional[int] = None) -> Optional[AiAsk]:
    query = _joined().where(AiAsk.id == ask_id)
    if user_id is not None:
        query = query.where(AiAsk.user_id == user_id)
    return query.first()


@db_operation("ai.ask_close")
def _close(ask_id: int, state: str, reason: Optional[str], now: datetime) -> bool:
    """A waiting ask closed without running (cancelled, missed, skipped, failed): one conditional write, so
    only a waiting one moves."""
    return (AiAsk.update(state=state, reason=reason, updated_at=now)
            .where(AiAsk.id == ask_id, AiAsk.state == PENDING).execute()) > 0


@db_operation("ai.ask_take")
def take(ask_id: int, checkin_id: int, now: datetime) -> bool:
    """The check-in that runs the ask takes it (checkins._start): one conditional write, so a cancel or
    another start that got there first wins and this check-in stands down."""
    return (AiAsk.update(state=STARTED, checkin_id=checkin_id, updated_at=now)
            .where(AiAsk.id == ask_id, AiAsk.state == PENDING).execute()) > 0


@db_operation("ai.asks_due")
def _due(now: datetime) -> list[AiAsk]:
    return list(AiAsk.select().where(AiAsk.state == PENDING, AiAsk.due_at <= now)
                .order_by(AiAsk.due_at, AiAsk.id).limit(BATCH))


@db_operation("ai.asks_of_team")
def _of_team(user_id: int, team_id: int, since: datetime) -> tuple[list[AiAsk], list[AiAsk]]:
    """The team's waiting asks, soonest first, and the others due since `since`, latest first."""
    base = _joined().where(AiAsk.user_id == user_id, AiAsk.team_id == team_id)
    waiting = list(base.where(AiAsk.state == PENDING).order_by(AiAsk.due_at, AiAsk.id))
    recent = list(_joined().where(AiAsk.user_id == user_id, AiAsk.team_id == team_id, AiAsk.state != PENDING,
                                  AiAsk.due_at >= since)
                  .order_by(AiAsk.due_at.desc(), AiAsk.id.desc()).limit(10))
    return waiting, recent


@db_operation("ai.asks_of_day")
def _of_day(user_id: int, team_id: int, start: datetime, end: datetime) -> list[AiAsk]:
    """The team's asks due in [start, end) that never ran (waiting, cancelled, missed, ...): the ones
    that ran are their check-ins."""
    return list(AiAsk.select().where(AiAsk.user_id == user_id, AiAsk.team_id == team_id, AiAsk.due_at >= start,
                                     AiAsk.due_at < end, AiAsk.state != STARTED)
                .order_by(AiAsk.due_at, AiAsk.id))


@db_operation("ai.asks_of_checkins")
def _of_checkins(checkin_ids: list[int]) -> dict[int, AiAsk]:
    if not checkin_ids:
        return {}
    return {a.checkin_id_id: a for a in _joined().where(AiAsk.checkin_id.in_(checkin_ids))}


@db_operation("ai.asks_by_id")
def _by_ids(ask_ids: list[int], user_id: int) -> dict[int, AiAsk]:
    if not ask_ids:
        return {}
    return {a.id: a for a in _joined().where(AiAsk.id.in_(ask_ids), AiAsk.user_id == user_id)}


# ------------------------------- the views ------------------------------- #


def state_of(row: AiAsk, checkin: Optional[AiCheckin]) -> str:
    """What the desk shows: the ask's own state, or once it ran, its check-in's (running, done, failed)."""
    if row.state != STARTED:
        return row.state
    if checkin is None:
        return DONE
    return checkin.state if checkin.state in (RUNNING, DONE, FAILED) else DONE


def view(row: AiAsk, checkin: Optional[AiCheckin] = None) -> dict[str, Any]:
    """An ask as the desk shows it (an `AiAskView`). `checkin` is the one that ran it, if it ran; left out,
    the row's own join is read."""
    ci = checkin if checkin is not None else _checkin_of(row)
    return {
        "ask_id": row.id,
        "team_id": row.team_id_id,
        "what": row.what,
        "due_at": _aware(row.due_at),
        "state": state_of(row, ci),
        "reason": row.reason,
        "created_at": _aware(row.created_at),
        "asked_in": row.conversation_id_id,
        "checkin_id": row.checkin_id_id,
        "conversation_id": ci.conversation_id_id if ci is not None else None,
    }


def block_of(v: dict[str, Any]) -> dict[str, Any]:
    """The chat's block for an ask (an `AiAskBlock` once agent._run_one gives it its id), JSON-ready: it is
    stored with the turn."""
    return {"ask_id": v["ask_id"], "team_id": v["team_id"], "what": v["what"], "due_at": v["due_at"].isoformat(),
            "state": v["state"], "reason": v["reason"], "checkin_id": v["checkin_id"],
            "conversation_id": v["conversation_id"]}


def _for_model(v: dict[str, Any]) -> dict[str, Any]:
    return {"ask_id": v["ask_id"], "team_id": v["team_id"], "at_et": et_words(v["due_at"]), "what": v["what"],
            "state": v["state"]}


# ------------------------------- what Dime calls ------------------------------- #


def _user_turn_only(checkin_id: Optional[int], what: str) -> None:
    if checkin_id is not None:
        raise AppError("AI_ASK_NOT_HERE", f"Only the user {what} an ask, in their own turn; this is a check-in")


async def schedule(user_id: int, conversation_id: Optional[int], checkin_id: Optional[int], team_id: int,
                   args: ScheduleAskInput, now: Optional[datetime] = None) -> dict[str, Any]:
    """Save the ask (the caller checked the team is the user's). Returns the model's view, with the chat's
    block under `ASK_KEY`."""
    _user_turn_only(checkin_id, "saves")
    if conversation_id is None:
        raise AppError("AI_NO_CONVERSATION", "An ask can only be saved inside a conversation")
    if not (settings.ai_enabled and settings.autopilot_enabled):
        raise AppError("AI_ASKS_OFF", "Dime's check-ins are switched off on the server, so an ask wouldn't run; "
                                      "tell the user")
    now = now or _now()
    at = args.at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if at <= now:
        raise AppError("AI_ASK_INVALID", f"{et_words(at)} ET has passed (it's {et_words(now)} ET now): do it now, "
                                         "or pick a later time")
    if at > now + AHEAD:
        raise AppError("AI_ASK_INVALID", "An ask can be at most two weeks ahead")
    if await _waiting(team_id) >= PER_TEAM:
        raise AppError("AI_ASK_LIMIT", f"This team already has {PER_TEAM} asks waiting: the user can cancel one "
                                       "on the AI desk, or you can with cancel_ask")
    row = await _insert(user_id, team_id, conversation_id, args.what, at, now)
    v = view(row)
    log.info("ai_ask_saved", ask_id=row.id, team_id=team_id, due_at=at.isoformat())
    return {**_for_model(v), "saved": True, "now_et": et_words(now), ASK_KEY: block_of(v)}


async def cancel_from_chat(user_id: int, checkin_id: Optional[int], team_id: int, args: CancelAskInput,
                           now: Optional[datetime] = None) -> dict[str, Any]:
    _user_turn_only(checkin_id, "cancels")
    row = await _get(args.ask_id, user_id)
    if row is None or row.team_id_id != team_id:
        raise AppError("AI_ASK_NOT_FOUND", f"Ask {args.ask_id} isn't one of this team's")
    v = await _cancel(row, now or _now())
    return {**_for_model(v), "cancelled": v["state"] == CANCELLED, ASK_KEY: block_of(v)}


async def _cancel(row: AiAsk, now: datetime) -> dict[str, Any]:
    if row.state != PENDING:
        raise ConflictError("AI_ASK_CLOSED", f"That ask is {state_of(row, _checkin_of(row))} already, so there is "
                                             "nothing to cancel")
    if not await _close(row.id, CANCELLED, "user", now):
        # It started (or closed) between the read and the write
        fresh = await _get(row.id)
        raise ConflictError("AI_ASK_CLOSED", f"That ask is {state_of(fresh, _checkin_of(fresh)) if fresh else 'gone'} "
                                             "already, so there is nothing to cancel")
    fresh = await _get(row.id)
    return view(fresh if fresh is not None else row)


# ------------------------------- the desk ------------------------------- #


async def team_asks(user_id: int, team_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """The team's waiting asks, soonest first, and the ones due in the last day, latest first (an
    `AiAsksData`). The caller checked the team is the user's."""
    now = now or _now()
    waiting, recent = await _of_team(user_id, team_id, now - timedelta(days=1))
    return {"team_id": team_id, "waiting": [view(a) for a in waiting], "recent": [view(a) for a in recent]}


async def cancel(user_id: int, ask_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """The user cancels a waiting ask from the desk."""
    row = await _get(ask_id, user_id)
    if row is None:
        raise NotFoundError("AI_ASK_NOT_FOUND", "Ask not found")
    return await _cancel(row, now or _now())


async def of_day(user_id: int, team_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [view(a, None) for a in await _of_day(user_id, team_id, start, end)]


async def of_checkins(checkin_ids: list[int]) -> dict[int, dict[str, Any]]:
    return {cid: view(a) for cid, a in (await _of_checkins(checkin_ids)).items()}


async def waiting_for_model(team_id: int, user_id: int) -> list[dict[str, Any]]:
    """The team's waiting asks as get_automations lists them."""
    waiting, _ = await _of_team(user_id, team_id, _now())
    return [_for_model(view(a)) for a in waiting]


def _is_ask(part: dict[str, Any]) -> bool:
    return part.get("kind") == "block" and isinstance(part.get("block"), dict) and part["block"].get("type") == "ask"


async def refresh_history(history: list[list[dict[str, Any]]], user_id: int) -> list[list[dict[str, Any]]]:
    """A conversation's turns with each ask block as it stands now (cancelled, ran, ...). One read covers
    every turn."""
    ids = sorted({p["block"].get("ask_id") for parts in history for p in parts if _is_ask(p)} - {None})
    if not ids:
        return history
    rows = await _by_ids(ids, user_id)

    def refresh(part: dict[str, Any]) -> dict[str, Any]:
        row = rows.get(part["block"].get("ask_id")) if _is_ask(part) else None
        if row is None:
            return part
        return {**part, "block": {**part["block"], **block_of(view(row))}}
    return [[refresh(p) for p in parts] for parts in history]


# ------------------------------- running them ------------------------------- #


async def run_one(row: AiAsk, now: datetime) -> Optional[int]:
    """Start the due ask's check-in: its id, or None when the team has one running (the ask waits for the
    next look) or the ask closed instead (missed, skipped, failed)."""
    from services.ai import checkins
    if _aware(row.due_at) < now - GRACE:
        await _close(row.id, MISSED, "late", now)
        return None
    team_id, user_id = row.team_id_id, row.user_id_id
    if not checkins.allowed(team_id, user_id) or not await checkins._owns(team_id, user_id):
        await _close(row.id, SKIPPED, "not_allowed", now)
        return None
    try:
        return await checkins.start_ask(row, now)
    except AppError as exc:
        if exc.error_code == "AI_AUTOPILOT_BUDGET_REACHED":
            await _close(row.id, SKIPPED, "budget", now)
            return None
        log.exception("ai_ask_start_failed", ask_id=row.id, team_id=team_id)
        await _close(row.id, FAILED, "start_failed", now)
        return None
    except Exception:
        log.exception("ai_ask_start_failed", ask_id=row.id, team_id=team_id)
        await _close(row.id, FAILED, "start_failed", now)
        return None


async def run_due(now: Optional[datetime] = None) -> dict[str, int]:
    """One look: start each due ask (one per team a look: a second would find the first running), close the
    ones past their grace as missed."""
    out = {"started": 0, "waiting": 0, "closed": 0}
    if not (settings.ai_enabled and settings.autopilot_enabled):
        return out
    now = now or _now()
    started_teams: set[int] = set()
    for row in await _due(now):
        if row.team_id_id in started_teams and _aware(row.due_at) >= now - GRACE:
            out["waiting"] += 1
            continue
        try:
            checkin_id = await run_one(row, now)
        except Exception:
            log.exception("ai_ask_run_failed", ask_id=row.id)
            continue
        if checkin_id is not None:
            out["started"] += 1
            started_teams.add(row.team_id_id)
            log.info("ai_ask_started", ask_id=row.id, team_id=row.team_id_id, checkin_id=checkin_id)
        else:
            fresh = await _get(row.id)
            if fresh is not None and fresh.state == PENDING:
                out["waiting"] += 1
            else:
                out["closed"] += 1
    return out


async def keep_running() -> None:
    """Every minute, the asks that fell due (from the app's lifespan, main.py). Logs its own failures and
    never stops on one."""
    while True:
        await asyncio.sleep(EVERY_SECONDS)
        try:
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ai_asks_look_failed")
