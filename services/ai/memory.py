"""
Dime's memory: the notes the agent keeps on a user's teams and leagues between
conversations (the Dime autopilot plan, step 1; usr.ai_memory, migration 0041).

Conversation history is append-only and replayed verbatim, so nothing the agent
learns in one conversation reaches the next. The notebook is that bridge: a
plan for the matchup, a thesis about a player with a date to recheck it, an
opponent's habit, the user's standing preferences, a lesson from the move
ledger. Four tools work it -- `recall` reads, `remember` writes, `revise`
replaces, `forget` retires -- and every turn's header carries a short digest
(the plans and the notes due for a recheck), in the user message, never in
`AGENT_PROMPT`.

Rules the tools enforce, not the prompt:

- **Never edited in place.** Revising a note retires it and writes the
  replacement (`replaces_id`); forgetting retires it. What the agent believed,
  and when, stays readable.
- **The user's notes outrank the agent's.** A note the user wrote (the AI desk's
  "What Dime knows") can't be revised or forgotten by the agent, and the user
  may revise or retire any note.
- **A cap.** At most `CAP` live notes per team (and `CAP` about the user), so
  the agent merges and retires instead of piling up. The user is never capped.
- **Judgments, not statistics.** A note may hold a number ("2 adds left"), but
  every number the agent states still has to come from a tool in that turn;
  the made-up-number check doesn't count notes as a source.
- **The user's switch.** With the account's memory feature off (services/ai/
  ai_settings.py) the four tools answer that memory is off, writing and reading
  nothing, and the digest is empty. They stay in the tool list so the cached
  prefix doesn't change. The user's own list and edits are not gated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.errors import AppError, NotFoundError
from db.base import db, run_db
from db.models.ai_conversations import AiMemory

KINDS = ("plan", "player", "league", "opponent", "preference", "lesson", "note")
Kind = Literal["plan", "player", "league", "opponent", "preference", "lesson", "note"]
CAP = 60
MAX_TEXT = 500
RECHECK_MAX_DAYS = 180
DIGEST_TEXT = 160          # a note's text in the header, at most
DIGEST_PLANS_PER_TEAM = 2
DIGEST_DUE = 5

RECALL_NAME = "recall"
REMEMBER_NAME = "remember"
REVISE_NAME = "revise"
FORGET_NAME = "forget"
MEMORY_TOOL_NAMES = frozenset({RECALL_NAME, REMEMBER_NAME, REVISE_NAME, FORGET_NAME})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- inputs ----

class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _clean_text(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise ValueError("a note needs some text")
    return text


class RecallInput(_Input):
    team_id: Optional[int] = Field(None, gt=0, lt=2**31)
    kind: Optional[Kind] = None
    query: Optional[str] = Field(None, min_length=2, max_length=80)
    include_retired: bool = False
    limit: int = Field(30, ge=1, le=CAP)


class RememberInput(_Input):
    kind: Kind
    text: str = Field(..., min_length=1, max_length=MAX_TEXT)
    team_id: Optional[int] = Field(None, gt=0, lt=2**31)
    recheck_on: Optional[date] = None

    _text = field_validator("text")(_clean_text)


class ReviseInput(_Input):
    note_id: int = Field(..., gt=0)
    text: Optional[str] = Field(None, min_length=1, max_length=MAX_TEXT)
    kind: Optional[Kind] = None
    recheck_on: Optional[date] = None
    clear_recheck: bool = False

    @field_validator("text")
    @classmethod
    def _text(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_text(value)


class ForgetInput(_Input):
    note_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=2, max_length=200)


# ---- tool definitions (appended to AGENT_TOOLS, in this order) ----

_KIND_HELP = (
    "plan (the user's plan for the season or the matchup), player (a thesis about one player), "
    "league (how the league works), opponent (another manager's habits), preference (how the user "
    "likes to manage), lesson (what the move ledger taught), note (anything else)"
)

RECALL: dict[str, Any] = {
    "name": RECALL_NAME,
    "description": (
        "Your notes on the user's teams and leagues, kept between conversations: their plans, theses "
        "about players, league and opponent habits, the user's preferences, and lessons from how moves "
        "went. The header shows the plans and the notes due for a recheck; recall reads the rest. Call "
        "it before advice that depends on what was decided or learned before. With team_id: that "
        "team's notes and the notes about the user. query matches words in the text. Notes marked "
        "author user were written by the user and outrank yours."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "query": {"type": "string", "description": "Words the note contains"},
            "include_retired": {"type": "boolean", "description": "Also revised and forgotten notes (default false)"},
            "limit": {"type": "integer", "description": f"1-{CAP} (default 30)"},
        },
        "required": [],
        "additionalProperties": False,
    },
}

REMEMBER: dict[str, Any] = {
    "name": REMEMBER_NAME,
    "description": (
        "Write a note you'll see in later conversations. Remember when the user states a plan or a "
        "preference, when you reach a judgment worth keeping (why you'd hold a player, what to watch "
        "for), or when the move ledger teaches something. One idea per note, a sentence or two, with "
        "the reason. Judgments and plans, not statistics: numbers you state later must still come "
        "from a tool. Give recheck_on when the note should be looked at again by a date. Kinds: "
        f"{_KIND_HELP}. Without team_id the note is about the user, for every team. At most {CAP} "
        "live notes per team: revise or forget old ones rather than piling up."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "text": {"type": "string", "description": f"The note, at most {MAX_TEXT} characters"},
            "team_id": {"type": "integer", "description": "The team it is about; leave out for the user"},
            "recheck_on": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
        },
        "required": ["kind", "text"],
        "additionalProperties": False,
    },
}

REVISE: dict[str, Any] = {
    "name": REVISE_NAME,
    "description": (
        "Replace one of your notes with a corrected or merged version (new text, kind or recheck date; "
        "clear_recheck drops the date). The old note is retired, not erased. Use it when a thesis "
        "changed, a plan moved, or to merge notes (revise one, forget the rest). Notes the user wrote "
        "can't be revised: say what you'd change instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer"},
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "recheck_on": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
            "clear_recheck": {"type": "boolean"},
        },
        "required": ["note_id"],
        "additionalProperties": False,
    },
}

FORGET: dict[str, Any] = {
    "name": FORGET_NAME,
    "description": (
        "Retire one of your notes that no longer holds (a plan that ended, a thesis the results "
        "disproved, a duplicate), with the reason. Notes the user wrote can't be forgotten by you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer"},
            "reason": {"type": "string", "description": "Why it no longer holds"},
        },
        "required": ["note_id", "reason"],
        "additionalProperties": False,
    },
}

MEMORY_TOOLS: list[dict[str, Any]] = [RECALL, REMEMBER, REVISE, FORGET]


# ---- views ----

def note_view(row: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "note_id": row.id,
        "kind": row.kind,
        "team_id": row.team_id_id,
        "text": row.text,
        "recheck_on": row.recheck_on,
        "author": row.author,
        "written_on": _date_of(row.created_at),
    }
    if row.retired_at is not None:
        view["retired_on"] = _date_of(row.retired_at)
        view["retired_by"] = row.retired_by
        view["retired_reason"] = row.retired_reason
    return view


def _date_of(value: Optional[datetime]) -> Optional[str]:
    return value.date().isoformat() if value is not None else None


def _quote(text: str, limit: int = DIGEST_TEXT) -> str:
    short = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return '"' + short.replace('"', "'") + '"'


def _check_recheck(recheck_on: Optional[date], today: date) -> None:
    if recheck_on is None:
        return
    if recheck_on < today:
        raise AppError("AI_MEMORY_RECHECK", f"recheck_on {recheck_on} is in the past (today is {today})")
    if recheck_on > today + timedelta(days=RECHECK_MAX_DAYS):
        raise AppError("AI_MEMORY_RECHECK", f"recheck_on is more than {RECHECK_MAX_DAYS} days away")


# ---- repository (synchronous; called through run_db) ----

def _team_filter(query, team_id: Optional[int], with_user_notes: bool):
    if team_id is None:
        return query
    if with_user_notes:
        return query.where((AiMemory.team_id == team_id) | (AiMemory.team_id.is_null()))
    return query.where(AiMemory.team_id == team_id)


def select_notes(user_id: int, *, team_id: Optional[int] = None, kind: Optional[str] = None,
                 query: Optional[str] = None, include_retired: bool = False, limit: int = 30) -> list[AiMemory]:
    q = AiMemory.select().where(AiMemory.user_id == user_id)
    q = _team_filter(q, team_id, with_user_notes=True)
    if not include_retired:
        q = q.where(AiMemory.retired_at.is_null())
    if kind is not None:
        q = q.where(AiMemory.kind == kind)
    for word in (query or "").split()[:6]:
        q = q.where(AiMemory.text.contains(word))       # ILIKE '%word%'
    return list(q.order_by(AiMemory.retired_at.is_null(False), AiMemory.created_at.desc()).limit(limit))


def count_live(user_id: int, team_id: Optional[int]) -> int:
    q = AiMemory.select().where((AiMemory.user_id == user_id) & AiMemory.retired_at.is_null())
    q = q.where(AiMemory.team_id.is_null()) if team_id is None else q.where(AiMemory.team_id == team_id)
    return q.count()


def get_note(note_id: int, user_id: int) -> Optional[AiMemory]:
    return AiMemory.get_or_none((AiMemory.id == note_id) & (AiMemory.user_id == user_id))


def insert_note(*, user_id: int, team_id: Optional[int], kind: str, text: str, recheck_on: Optional[date],
                author: str, conversation_id: Optional[int], replaces_id: Optional[int] = None) -> AiMemory:
    return AiMemory.create(user_id=user_id, team_id=team_id, kind=kind, text=text, recheck_on=recheck_on,
                           author=author, conversation_id=conversation_id, replaces_id=replaces_id,
                           created_at=_now())


def retire_note(note_id: int, user_id: int, *, by: str, reason: str) -> bool:
    """Retire a live note; False when it was already retired (or isn't the user's)."""
    return AiMemory.update(retired_at=_now(), retired_by=by, retired_reason=reason).where(
        (AiMemory.id == note_id) & (AiMemory.user_id == user_id) & AiMemory.retired_at.is_null()
    ).execute() == 1


def replace_note(old: AiMemory, *, kind: str, text: str, recheck_on: Optional[date], author: str,
                 conversation_id: Optional[int]) -> Optional[AiMemory]:
    """Retire `old` and write its replacement, together. None when `old` was retired
    in the meantime (a race with another turn or the user)."""
    with db.atomic():
        if not retire_note(old.id, old.user_id_id, by=author, reason="revised"):
            return None
        return insert_note(user_id=old.user_id_id, team_id=old.team_id_id, kind=kind, text=text,
                           recheck_on=recheck_on, author=author, conversation_id=conversation_id,
                           replaces_id=old.id)


def live_for_digest(user_id: int) -> list[AiMemory]:
    return list(AiMemory.select().where((AiMemory.user_id == user_id) & AiMemory.retired_at.is_null())
                .order_by(AiMemory.created_at.desc()))


# ---- the agent's tools ----

@dataclass(frozen=True)
class Caller:
    user_id: int
    conversation_id: Optional[int]
    today: date


OFF = {"memory": "off", "note": "The user turned Dime's memory off (AI and automation settings): you keep and "
                                "read no notes, so answer without them and don't say you'll remember"}


async def _off(user_id: int) -> bool:
    from services.ai import ai_settings
    return not (await ai_settings.get(user_id)).memory


async def recall(caller: Caller, args: RecallInput) -> dict[str, Any]:
    if await _off(caller.user_id):
        return dict(OFF)
    rows = await run_db("ai.memory_recall", select_notes, caller.user_id, team_id=args.team_id, kind=args.kind,
                        query=args.query, include_retired=args.include_retired, limit=args.limit)
    live = await run_db("ai.memory_count", count_live, caller.user_id, args.team_id)
    out: dict[str, Any] = {"notes": [note_view(r) for r in rows], "cap": CAP}
    if args.team_id is not None:
        out["live_on_team"] = live
    if not rows:
        out["note"] = "No notes match." if (args.query or args.kind) else "No notes yet."
    return out


async def remember(caller: Caller, args: RememberInput) -> dict[str, Any]:
    if await _off(caller.user_id):
        return dict(OFF)
    _check_recheck(args.recheck_on, caller.today)
    live = await run_db("ai.memory_count", count_live, caller.user_id, args.team_id)
    if live >= CAP:
        where = f"team {args.team_id}" if args.team_id is not None else "the user"
        raise AppError("AI_MEMORY_FULL", f"{where} already has {CAP} live notes: revise or forget one first")
    row = await run_db("ai.memory_insert", insert_note, user_id=caller.user_id, team_id=args.team_id,
                       kind=args.kind, text=args.text, recheck_on=args.recheck_on, author="dime",
                       conversation_id=caller.conversation_id)
    return {"note_id": row.id, "live_notes": live + 1, "cap": CAP}


async def _own_note(caller: Caller, note_id: int) -> AiMemory:
    row = await run_db("ai.memory_get", get_note, note_id, caller.user_id)
    if row is None:
        raise AppError("AI_MEMORY_NOT_FOUND", f"No note {note_id}")
    if row.retired_at is not None:
        raise AppError("AI_MEMORY_RETIRED", f"Note {note_id} was already {('revised' if row.retired_reason == 'revised' else 'forgotten')}: recall for the live one")
    if row.author == "user":
        raise AppError("AI_MEMORY_USERS", f"Note {note_id} is the user's own: you can't change it. Say what you'd change instead")
    return row


async def revise(caller: Caller, args: ReviseInput) -> dict[str, Any]:
    if await _off(caller.user_id):
        return dict(OFF)
    old = await _own_note(caller, args.note_id)
    recheck = None if args.clear_recheck else (args.recheck_on if args.recheck_on is not None else old.recheck_on)
    if args.recheck_on is not None:
        _check_recheck(args.recheck_on, caller.today)
    text = args.text if args.text is not None else old.text
    kind = args.kind if args.kind is not None else old.kind
    if text == old.text and kind == old.kind and recheck == old.recheck_on:
        raise AppError("AI_MEMORY_UNCHANGED", "Nothing to change: give new text, a kind or a recheck date")
    new = await run_db("ai.memory_replace", replace_note, old, kind=kind, text=text, recheck_on=recheck,
                       author="dime", conversation_id=caller.conversation_id)
    if new is None:
        raise AppError("AI_MEMORY_RETIRED", f"Note {args.note_id} was changed meanwhile: recall it again")
    return {"note_id": new.id, "replaced": old.id}


async def forget(caller: Caller, args: ForgetInput) -> dict[str, Any]:
    if await _off(caller.user_id):
        return dict(OFF)
    old = await _own_note(caller, args.note_id)
    if not await run_db("ai.memory_retire", retire_note, old.id, caller.user_id, by="dime",
                        reason=" ".join(args.reason.split())):
        raise AppError("AI_MEMORY_RETIRED", f"Note {args.note_id} was changed meanwhile: recall it again")
    return {"forgotten": old.id}


# ---- the header digest ----

def digest_of(rows: list[Any], today: date) -> str:
    """One line for the header: how many live notes, each team's newest plans, and the
    notes due for a recheck. Empty when there are no notes."""
    if not rows:
        return ""
    parts = [f"Your notes: {len(rows)} live (recall reads them)."]
    plans: dict[Optional[int], list[Any]] = {}
    for r in rows:                                   # newest first
        if r.kind == "plan":
            plans.setdefault(r.team_id_id, []).append(r)
    if plans:
        bits = []
        for team_id in sorted(plans, key=lambda t: (t is not None, t or 0)):
            who = f"team_id {team_id}" if team_id is not None else "the user"
            notes = "; ".join(_quote(r.text) for r in plans[team_id][:DIGEST_PLANS_PER_TEAM])
            bits.append(f"{who}: {notes}")
        parts.append("Plans: " + " · ".join(bits) + ".")
    due = sorted((r for r in rows if r.recheck_on is not None and r.recheck_on <= today),
                 key=lambda r: (r.recheck_on, r.id))
    if due:
        bits = [f"note {r.id}" + (f" (team_id {r.team_id_id})" if r.team_id_id is not None else "")
                + f" {_quote(r.text)}" for r in due[:DIGEST_DUE]]
        more = f" and {len(due) - DIGEST_DUE} more" if len(due) > DIGEST_DUE else ""
        parts.append("Due for a recheck: " + "; ".join(bits) + more + ".")
    return " ".join(parts)


async def digest(user_id: int, today: date) -> str:
    """The header's line; empty with no notes or with the user's memory off."""
    if await _off(user_id):
        return ""
    rows = await run_db("ai.memory_digest", live_for_digest, user_id)
    return digest_of(rows, today)


_DIGEST = re.compile(r"Your notes: .*")
_QUOTED = re.compile(r'"[^"]*"')


def without_note_texts(content: str) -> str:
    """A stored user message with the digest's quoted note texts emptied, for grounding.

    The turn header (the message's first line) carries the digest, and stored history
    keeps it. A number in a note is not a source for the user's stats (the agent wrote
    it, or it is the user's plan from another day), so the answer must get it from a
    tool again. Note texts are quoted with no double quote inside (`_quote`) and no
    line break (`_clean_text`), and the rest of the header (team ids, note ids) stays."""
    first, sep, rest = content.partition("\n")
    if not first.startswith("["):
        return content
    return _DIGEST.sub(lambda m: _QUOTED.sub('""', m.group(0)), first, count=1) + sep + rest


# ---- the user's own edits (the AI desk's "What Dime knows") ----

async def list_for_user(user_id: int, team_id: Optional[int], include_retired: bool = False) -> list[dict[str, Any]]:
    rows = await run_db("ai.memory_list", select_notes, user_id, team_id=team_id, include_retired=include_retired,
                        limit=200)
    return [note_view(r) for r in rows]


async def add_for_user(user_id: int, *, kind: str, text: str, team_id: Optional[int],
                       recheck_on: Optional[date]) -> dict[str, Any]:
    row = await run_db("ai.memory_insert", insert_note, user_id=user_id, team_id=team_id, kind=kind,
                       text=_clean_text(text), recheck_on=recheck_on, author="user", conversation_id=None)
    return note_view(row)


async def edit_for_user(user_id: int, note_id: int, *, text: Optional[str], kind: Optional[str],
                        recheck_on: Optional[date], clear_recheck: bool) -> dict[str, Any]:
    old = await run_db("ai.memory_get", get_note, note_id, user_id)
    if old is None or old.retired_at is not None:
        raise NotFoundError("AI_MEMORY_NOT_FOUND", "Note not found")
    recheck = None if clear_recheck else (recheck_on if recheck_on is not None else old.recheck_on)
    new = await run_db("ai.memory_replace", replace_note, old, kind=kind or old.kind,
                       text=_clean_text(text) if text is not None else old.text, recheck_on=recheck,
                       author="user", conversation_id=None)
    if new is None:
        raise NotFoundError("AI_MEMORY_NOT_FOUND", "Note not found")
    return note_view(new)


async def delete_for_user(user_id: int, note_id: int) -> None:
    if not await run_db("ai.memory_retire", retire_note, note_id, user_id, by="user", reason="deleted by the user"):
        raise NotFoundError("AI_MEMORY_NOT_FOUND", "Note not found")
