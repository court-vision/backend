"""
The AI agent's turn: one user message in a conversation, answered with the
tools and streamed as it happens (docs/AI_AGENT_PLAN.md § 1).

A turn is the router's bounded loop grown up:

- **History.** The request is the conversation's earlier successful turns,
  replayed verbatim (services/ai/conversations.py), plus this turn's messages.
  Nothing earlier is ever edited, so the prompt cache reads it back and Claude
  Opus 5.5 accepts the thinking blocks in it.
- **Bounds.** At most `ai_agent_max_model_calls` model calls and
  `ai_agent_max_tool_calls` tool calls, and once the turn has spent
  `ai_agent_turn_max_usd` its next call is its last. The last call is made with
  `tool_choice: none`, so a turn always ends in an answer.
- **Streaming.** Prose streams as the model writes it; tool calls and the
  model's progress notes become status lines; display tools become blocks (a
  table, a chart or a place in the app) once the server has built them. Events go on a queue the route drains as SSE.
- **Detached.** The turn runs in its own task. A client that disconnects stops
  hearing it, not running it: the tokens are already being spent, and the turn
  must still be written so the conversation stays whole.

Every turn writes one usr.ai_turns row, failed or not (a failed turn can
still have cost money), and logs one `ai_turn` line.

A draft note (services/ai/draft.py) is the same again, started from a draft room:
the server's brief (with the room's board already read), the draft tool list
(`set_draft_plan` and `draft_pick` added) and a short leash (`Limits.draft`).

A check-in (services/ai/checkins.py) is the same turn started by the server
instead of the user: its opening message is the server's brief, it runs with
the check-in's tool list (the agent's plus `log_decision`) and its own bounds
(`Limits.checkin`), and its proposals are stored in shadow. Every turn of a
check-in's conversation uses that tool list, so its replayed history always
matches the tools it was written with.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import anthropic

from core.errors import AppError, ProviderError
from core.logging import get_logger
from core.nba_calendar import EASTERN, nba_date_et
from core.settings import settings
from core.tracing import timed
from schemas.ai import AiToolCall
from services.ai import conversations, memory, proposals, spend
from services.ai.client import get_client
from services.ai.decisions import DECISION_KEY, LOG_DECISION_NAME
from services.ai.display import (
    DISPLAY_TOOL_NAMES, SHOW_CHART, SHOW_PLACE, SHOW_TABLE, DisplayError, show_chart, show_place, show_table,
)
from services.ai.prompts import AGENT_PROMPT
from services.ai.proposals import CHIP_KEY
from services.ai.service import FALLBACK_BETA, _names_in, _Run, _season_line, _served_content, map_sdk_error
from services.ai.tools import (
    AGENT_TOOL_NAMES, AGENT_TOOLS, CHECKIN_ONLY_TOOLS, CHECKIN_TOOL_NAMES, DRAFT_ONLY_TOOLS, DRAFT_TOOL_NAMES,
    STEERING_TOOLS, ToolContext, list_my_teams, run_tool,
)

# Progress notes between tool calls come back as `thinking` blocks on Opus 5.5;
# "updates" returns a short summary of each one and keeps the reasoning hidden.
UPDATES_BETA = "thinking-display-updates-2026-08-18"
# A replayed thinking block whose history no longer matches (the tools or the
# prompt changed in a deploy since the conversation began) is dropped, and the
# conversation carries on without that reasoning, instead of the turn failing.
BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING = {"type": "adaptive", "display": "updates", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
MAX_TOKENS = 32_000  # per call: thinking counts toward it, and a cut-off answer is a failed turn
BUDGET_SPENT = "Not run: this turn's lookup budget is used up. Answer with what you have."
# Dime's steering tools (steer, set_lane, cancel_pickup) come after the display tools
_STEERING: list[dict[str, Any]] = STEERING_TOOLS
_STEERING_NAMES = frozenset(t["name"] for t in _STEERING)
TOOLS: list[dict[str, Any]] = [*AGENT_TOOLS, SHOW_TABLE, SHOW_CHART, SHOW_PLACE, *_STEERING]
ALLOWED = AGENT_TOOL_NAMES | _STEERING_NAMES
# A check-in's conversation: the same tools, then the check-in's own, appended so the
# rest of the prefix is the AI desk's
CHECKIN_TOOLS: list[dict[str, Any]] = [*TOOLS, *CHECKIN_ONLY_TOOLS]
CHECKIN_ALLOWED = CHECKIN_TOOL_NAMES | _STEERING_NAMES
# A draft note's conversation (services/ai/draft.py): the same tools, then the room's plan and the
# mock seat's pick, appended the same way
DRAFT_TOOLS: list[dict[str, Any]] = [*TOOLS, *DRAFT_ONLY_TOOLS]
DRAFT_ALLOWED = DRAFT_TOOL_NAMES | _STEERING_NAMES

# What the user sees while a tool runs
STATUS: dict[str, str] = {
    "search_players": "Looking up players",
    "get_player_stats": "Reading player stats",
    "get_player_status": "Checking injury reports",
    "get_my_teams": "Reading your teams",
    "get_matchup": "Reading your matchup",
    "get_free_agents": "Reading free agents",
    "get_game_log": "Reading box scores",
    "get_scheduled_pickups": "Reading your scheduled pickups",
    "get_projections": "Reading projections",
    "get_draft_market": "Reading the draft market",
    "get_market_movement": "Reading draft market moves",
    "get_schedule": "Reading the schedule",
    "get_draft_rooms": "Reading your draft rooms",
    "get_draft_room": "Reading the draft room",
    "query_stats": "Computing stats",
    "project_matchup": "Projecting the matchup",
    "plan_streams": "Planning streams",
    "propose_move": "Checking the move",
    "get_lineup": "Reading your lineup",
    "propose_lineup": "Checking the lineup",
    "find_in_app": "Looking through the app",
    "recall": "Reading its notes",
    "remember": "Making a note",
    "revise": "Updating a note",
    "forget": "Retiring a note",
    "get_move_outcomes": "Checking how moves went",
    "get_league": "Reading the league",
    "get_league_activity": "Reading the league's moves",
    "get_draft_board": "Reading the draft board",
    "get_automations": "Reading what the automations will do",
    "get_decisions": "Reading its earlier calls",
    "log_decision": "Logging a call",
    "get_player_news": "Reading the news",
    "get_news": "Reading the news",
    "get_draft_recap": "Reading the draft recap",
    "set_draft_plan": "Setting the draft plan",
    "draft_pick": "Making the pick",
    "steer": "Checking the holds",
    "set_lane": "Checking the lane change",
    "cancel_pickup": "Checking the pickup",
    "show_table": "Building a table",
    "show_chart": "Drawing a chart",
    "show_place": "Pointing out where",
}


@dataclass(frozen=True)
class Limits:
    """One turn's bounds: model calls, tool calls, wall clock, and the spend past which its next
    model call is its last."""

    model_calls: int
    tool_calls: int
    timeout_seconds: float
    max_usd: float

    @classmethod
    def chat(cls) -> "Limits":
        return cls(settings.ai_agent_max_model_calls, settings.ai_agent_max_tool_calls,
                   settings.ai_agent_turn_timeout_seconds, settings.ai_agent_turn_max_usd)

    @classmethod
    def checkin(cls) -> "Limits":
        """A check-in reads far more than a chat answer (memory, the automations, the board, the
        matchup, the ledger) and nobody waits on it, so it gets more steps and more time. The
        spend ceiling is the same."""
        return cls(settings.autopilot_checkin_max_model_calls, settings.autopilot_checkin_max_tool_calls,
                   settings.autopilot_checkin_timeout_seconds, settings.ai_agent_turn_max_usd)

    @classmethod
    def draft(cls) -> "Limits":
        """A draft note answers one question with the board already in its brief, while the user waits
        at the pick, so it gets fewer steps than a chat answer and a shorter clock."""
        return cls(settings.dime_draft_max_model_calls, settings.dime_draft_max_tool_calls,
                   settings.dime_draft_timeout_seconds, settings.ai_agent_turn_max_usd)


@dataclass(frozen=True)
class TurnResult:
    turn_id: Optional[int]
    outcome: str
    cost: int


@dataclass
class Turn:
    """One turn's state: what it appended, what it showed, what it found."""

    conversation_id: int
    user_id: int
    message: str
    history: conversations.History
    # The frontend build the user has open (find_in_app reads its app map)
    build: Optional[str] = None
    # The spend reserved for this turn (usr.ai_spend), settled when it is written
    reservation_id: Optional[int] = None
    # The tools it may call: the AI desk's, or a check-in conversation's
    tools: list[dict[str, Any]] = field(default_factory=lambda: TOOLS)
    allowed: frozenset[str] = ALLOWED
    # The check-in this turn is (usr.ai_checkins): its proposals are shadow, and it logs decisions
    checkin_id: Optional[int] = None
    # The draft note this turn writes (usr.ai_draft_notes): only it may set the room's plan or pick
    draft_note_id: Optional[int] = None
    limits: Limits = field(default_factory=Limits.chat)
    run: _Run = field(default_factory=_Run)
    messages: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    parts: list[dict[str, Any]] = field(default_factory=list)
    # The proposals (usr.ai_proposals ids) this turn made, to be tied to its row
    proposals: list[int] = field(default_factory=list)
    cost: int = 0
    context_tokens: int = 0
    tables: int = 0
    charts: int = 0
    chips: int = 0
    places: int = 0
    decisions: int = 0

    def all_results(self) -> dict[str, Any]:
        return {**self.history.results, **self.results}

    def next_result_id(self) -> str:
        return f"r{len(self.history.results) + len(self.results) + 1}"

    def add_text(self, delta: str) -> None:
        if self.parts and self.parts[-1]["kind"] == "text":
            self.parts[-1]["text"] += delta
        else:
            self.parts.append({"kind": "text", "text": delta})

    def answer_text(self) -> str:
        return "".join(p["text"] for p in self.parts if p["kind"] == "text")


class Emitter:
    """Events for the stream. Never blocks the turn: an unbounded queue, and
    nothing is lost if no one is listening any more."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Optional[tuple[str, dict[str, Any]]]] = asyncio.Queue()

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        self.queue.put_nowait((event, data))

    def close(self) -> None:
        self.queue.put_nowait(None)


# ---- the header each user message carries ----

# How the header names a points league's scoring (`tools.points_scoring`): ESPN's
# default is Court Vision's fpts, so the agent can quote fpts there as the league's own
_POINTS_SCORING = {"espn_default": "ESPN default points", "custom": "its own point values"}


def _team_facts(teams: list[dict[str, Any]], pinned: Optional[int]) -> str:
    if not teams:
        return "The user has no teams yet."

    def scoring(t: dict[str, Any]) -> str:
        named = _POINTS_SCORING.get(t.get("points_scoring") or "")
        return f"{t['scoring'] or 'unknown'} scoring" + (f": {named}" if named else "")
    facts = [f"team_id {t['team_id']} ({t['provider']}, {scoring(t)}, {t['season']})" for t in teams]
    line = "The user's teams: " + "; ".join(facts) + "."
    if pinned is not None and any(t["team_id"] == pinned for t in teams):
        line += f" Pinned team: team_id {pinned}."
    return line


async def header(user_id: int, pinned: Optional[int], now: Optional[datetime] = None, *,
                 conversation_id: Optional[int] = None) -> str:
    """Today, the season, and the user's teams, by ID and format only: names are
    league data, and the model can read them with get_my_teams. Then the digest of
    the agent's notes (its plans and the notes due for a recheck; services/ai/memory.py).
    In a conversation, what became of its earlier proposals (sent, dismissed, expired)
    comes last."""
    today = nba_date_et() if now is None else now.astimezone(EASTERN).date()
    try:
        teams = await list_my_teams(user_id)
    except Exception:
        get_logger().warning("ai_turn_header_teams_failed", user_id=user_id)
        teams = []
    season = await _season_line()
    notes = ""
    try:
        notes = await memory.digest(user_id, today)
    except Exception:
        get_logger().warning("ai_turn_header_memory_failed", user_id=user_id)
    chips = ""
    if conversation_id is not None:
        try:
            chips = await proposals.header_line(conversation_id, user_id, now)
        except Exception:
            get_logger().warning("ai_turn_header_proposals_failed", conversation_id=conversation_id)
    line = f"Today is {today.strftime('%A')} {today.isoformat()} (US Eastern). {season} {_team_facts(teams, pinned)}"
    return "[" + " ".join(part for part in (line, notes, chips) if part) + "]"


# ---- one model call, streamed ----

async def _stream_call(turn: Turn, emit: Emitter, *, final: bool, messages: list[dict[str, Any]]) -> Any:
    notes: dict[int, str] = {}
    try:
        with timed("claude", "POST claude messages (stream)"):
            async with get_client().beta.messages.stream(
                model=settings.ai_agent_model,
                max_tokens=MAX_TOKENS,
                # The fixed prefix (tools + system) gets its own breakpoint, shared by
                # every conversation; the top-level one caches each conversation so far.
                system=[{"type": "text", "text": AGENT_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=turn.tools,
                messages=messages,
                thinking=THINKING,
                output_config={"effort": settings.ai_agent_effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA, UPDATES_BETA, BINDING_BETA],
                fallbacks="default",
                **({"tool_choice": {"type": "none"}} if final else {}),
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "text":
                        # A new text block (the answer after a lookup) starts a new paragraph
                        if turn.answer_text() and not turn.answer_text().endswith("\n"):
                            turn.add_text("\n\n")
                            emit("text", {"delta": "\n\n"})
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta" and delta.text:
                            turn.add_text(delta.text)
                            emit("text", {"delta": delta.text})
                        elif delta.type == "thinking_delta" and delta.thinking:
                            notes[event.index] = notes.get(event.index, "") + delta.thinking
                    elif event.type == "content_block_stop" and notes.get(event.index, "").strip():
                        emit("status", {"text": notes.pop(event.index).strip(), "kind": "note"})
                return await stream.get_final_message()
    except anthropic.AnthropicError as exc:
        raise map_sdk_error(exc) from exc


# ---- tools ----

async def _run_one(turn: Turn, block: Any, emit: Emitter) -> dict[str, Any]:
    name, raw = block.name, block.input
    emit("status", {"text": STATUS.get(name, "Working"), "kind": "tool", "tool": name})
    if name in DISPLAY_TOOL_NAMES:
        try:
            if name == SHOW_CHART["name"]:
                shown = show_chart(raw, turn.all_results(), f"c{turn.charts + 1}")
                turn.charts += 1
            elif name == SHOW_PLACE["name"]:
                shown = show_place(raw, turn.all_results(), f"pl{turn.places + 1}", build=turn.build)
                turn.places += 1
            else:
                shown = show_table(raw, turn.all_results(), f"t{turn.tables + 1}")
                turn.tables += 1
        except DisplayError as exc:
            return {"type": "tool_result", "tool_use_id": block.id, "content": str(exc), "is_error": True}
        turn.parts.append({"kind": "block", "block": shown.block})
        emit("block", shown.block)
        return {"type": "tool_result", "tool_use_id": block.id, "content": shown.summary}

    outcome = await run_tool(name, raw, ctx=ToolContext(user_id=turn.user_id, conversation_id=turn.conversation_id,
                                                        build=turn.build, checkin_id=turn.checkin_id,
                                                        draft_note_id=turn.draft_note_id),
                             allowed=turn.allowed)
    if outcome.is_error:
        return {"type": "tool_result", "tool_use_id": block.id, "content": outcome.content, "is_error": True}
    result_id = turn.next_result_id()
    data = json.loads(outcome.content)
    chip = data.pop(CHIP_KEY, None)
    if chip is not None:
        # A proposal: the chip (ESPN ids and all) is the user's block, never the model's result
        turn.chips += 1
        shown = {"type": "proposal", "id": f"p{turn.chips}", **chip}
        turn.parts.append({"kind": "block", "block": shown})
        turn.proposals.append(chip["proposal_id"])
        emit("block", shown)
    decision = data.pop(DECISION_KEY, None)
    if decision is not None:
        # A check-in's call: shown in its conversation for the user to agree or disagree with
        turn.decisions += 1
        shown = {"type": "decision", "id": f"d{turn.decisions}", **decision}
        turn.parts.append({"kind": "block", "block": shown})
        emit("block", shown)
    turn.results[result_id] = {"tool": name, "input": raw if isinstance(raw, dict) else {}, "data": data}
    turn.run.names |= _names_in(outcome.content)
    return {"type": "tool_result", "tool_use_id": block.id,
            "content": json.dumps({"result_id": result_id, **data}, default=str, ensure_ascii=False)}


async def _run_tools(turn: Turn, tool_uses: list[Any], emit: Emitter) -> list[dict[str, Any]]:
    """Every tool_use gets a tool_result, all in one user message. Lookups run
    together; display calls run after them, in the order the model made them."""
    budget = max(turn.limits.tool_calls - len(turn.run.tool_calls), 0)
    runnable, over = tool_uses[:budget], tool_uses[budget:]
    lookups = [b for b in runnable if b.name not in DISPLAY_TOOL_NAMES]
    displays = [b for b in runnable if b.name in DISPLAY_TOOL_NAMES]
    by_id: dict[str, dict[str, Any]] = {}
    for block, result in zip(lookups, await asyncio.gather(*(_run_one(turn, b, emit) for b in lookups))):
        by_id[block.id] = result
    for block in displays:
        by_id[block.id] = await _run_one(turn, block, emit)
    for block in over:
        by_id[block.id] = {"type": "tool_result", "tool_use_id": block.id, "content": BUDGET_SPENT, "is_error": True}
    for block in tool_uses:
        raw = block.input if isinstance(block.input, dict) else {}
        turn.run.tool_calls.append(AiToolCall(name=block.name, input=raw, is_error=bool(by_id[block.id].get("is_error"))))
    return [by_id[block.id] for block in tool_uses]


# ---- the made-up-number check ----

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{4}-\d{2}\b")


def _numbers(text: str) -> list[str]:
    return _NUMBER.findall(text)


def _source_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
    elif isinstance(value, str):
        out.update(float(n) for n in _numbers(value))
    elif isinstance(value, dict):
        for item in value.values():
            _source_numbers(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _source_numbers(item, out)


def ungrounded_numbers(text: str, sources: list[Any], names: set[str] = frozenset()) -> int:
    """Numbers in the answer that no source supports. A number is supported when
    a source number rounds to it at the precision it was written with, or does
    as a percentage (0.523 -> 52.3%). Dates and names ("76ers") are removed
    first. Logged, not blocked; the eval is where it fails."""
    for name in sorted((n for n in names if n), key=len, reverse=True):
        text = re.sub(rf"(?<!\d){re.escape(name)}(?!\d)", " ", text, flags=re.IGNORECASE)
    text = _DATE.sub(" ", text)
    known: set[float] = set()
    for source in sources:
        _source_numbers(source, known)
    known |= {k * 100 for k in known if -1 <= k <= 1}

    def supported(written: str) -> bool:
        places = len(written.split(".")[1]) if "." in written else 0
        value = float(written)
        return any(round(k, places) == value for k in known)

    return sum(1 for n in _numbers(text) if not supported(n))


# ---- the made-up-place check ----

FIND_IN_APP = "find_in_app"
# Results whose numbers never ground an answer's: the app map's sample data, notes, and the
# check-in's own logged calls (their summary and reason are the model's words)
NOT_SOURCES = frozenset({FIND_IN_APP, *memory.MEMORY_TOOL_NAMES, LOG_DECISION_NAME})
# What an answer names in the app: a bold or quoted span of a few words
_NAMED = re.compile(r"\*\*([^*\n]{1,60})\*\*|\"([^\"\n]{1,60})\"|“([^”\n]{1,60})”")
_MAX_NAME_WORDS = 6
# A span is a place when the answer sends the user to it: a verb of getting
# somewhere just before it ("open **Market**", "tap \"Send\"") or a word for a
# part of the app just after it ("the **Market** view"), or a step in a trail
# ("Week › **Market**"). Bold for emphasis ("**Hold** him", "**Add Kessler**")
# is none of these.
_GO = frozenset(
    "click clicking tap tapping press pressing hit open opening select choose toggle switch flip "
    "go head navigate visit find use under".split()
)
_PART = frozenset(
    "tab tabs button buttons desk desks menu view views panel pane page screen sheet dialog link toggle "
    "switch key shortcut option options setting settings section tray slider field box column lens "
    "mode list rail".split()
)
_BEFORE, _AFTER = 4, 3
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")
_SENTENCE_END = re.compile(r"[.!?:;]\s|\n")


def _text_of(value: Any) -> str:
    return (value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)).casefold()


def _sends_there(text: str, start: int, end: int) -> bool:
    """Whether the span at text[start:end] is somewhere the answer sends the user."""
    before = text[:start]
    cut = max((m.end() for m in _SENTENCE_END.finditer(before)), default=0)
    before = before[cut:]
    after = text[end:]
    stop = _SENTENCE_END.search(after)
    after = after[:stop.start()] if stop else after
    if before.rstrip().endswith(("›", ">")) or after.lstrip().startswith(("›", ">")):
        return True
    words_before = [w.casefold() for w in _WORD.findall(before)][-_BEFORE:]
    words_after = [w.casefold() for w in _WORD.findall(after)][:_AFTER]
    return any(w in _GO for w in words_before) or any(w in _PART for w in words_after)


def placed_names(text: str) -> list[str]:
    """The bold or quoted names an answer sends the user to, as written."""
    out = []
    for match in _NAMED.finditer(text):
        group = next(i for i, g in enumerate(match.groups(), start=1) if g is not None)
        name = match.group(group).strip().strip(".,:;!?")
        if not name or len(name.split()) > _MAX_NAME_WORDS or not re.search(r"[^\W\d_]", name):
            continue
        if _sends_there(text, match.start(), match.end()):
            out.append(name)
    return out


def made_up_places(text: str, app: list[Any], others: list[Any]) -> list[str]:
    """The places an answer names (`placed_names`) that appear in no find_in_app
    result of the conversation (docs/APP_MAP_PLAN.md § 5): a button or screen the
    model made up. A name the other lookups or the user's own words contain (a
    player's, a category) is data, not a place."""
    known = [_text_of(v) for v in [*app, *others]]
    return [name for name in placed_names(text) if not any(name.casefold() in k for k in known)]


def ungrounded_places(text: str, app: list[Any], others: list[Any]) -> int:
    """How many places the answer made up. Logged, not blocked; the eval is where it fails."""
    return len(made_up_places(text, app, others))


# ---- the turn ----

async def run_turn(turn: Turn, emit: Emitter, *, header_line: str) -> TurnResult:
    """Run one turn to the end, write it, and close the stream. Never raises:
    failures become an `error` event and the turn's outcome, which it returns."""
    started = time.monotonic()
    outcome = "INTERNAL_ERROR"
    turn_id: Optional[int] = None
    ungrounded: Optional[int] = None
    places: Optional[int] = None
    made_up: list[str] = []
    user_content = f"{header_line}\n\n{turn.message}"
    turn.messages.append({"role": "user", "content": user_content})
    try:
        limits = turn.limits
        async with asyncio.timeout(limits.timeout_seconds):
            for call in range(1, limits.model_calls + 1):
                if call > 1:
                    # A new step: whatever the last status said (a table, a lookup) is done
                    emit("status", {"text": "Thinking", "kind": "step"})
                final = (call == limits.model_calls
                         or len(turn.run.tool_calls) >= limits.tool_calls
                         or turn.cost >= round(limits.max_usd * 1_000_000))
                response = await _stream_call(turn, emit, final=final,
                                              messages=[*turn.history.messages, *turn.messages])
                turn.run.record(response)
                turn.cost = spend.cost_micros(
                    turn.run.model or settings.ai_agent_model,
                    input_tokens=turn.run.input_tokens, output_tokens=turn.run.output_tokens,
                    cache_read_input_tokens=turn.run.cache_read_input_tokens,
                    cache_creation_input_tokens=turn.run.cache_creation_input_tokens)
                usage = response.usage
                turn.context_tokens = ((usage.input_tokens or 0) + (usage.cache_read_input_tokens or 0)
                                       + (usage.cache_creation_input_tokens or 0))

                if response.stop_reason == "refusal":
                    raise AppError("AI_DECLINED", "The assistant can't help with that", status_code=422,
                                   log_level="warning")
                if response.stop_reason == "max_tokens":
                    raise ProviderError("anthropic", "The answer was cut off; try a narrower question",
                                        error_code="AI_INCOMPLETE")

                content = _served_content(response.content)
                turn.messages.append({"role": "assistant",
                                      "content": [block.to_dict() for block in content]})
                tool_uses = [block for block in content if block.type == "tool_use"]
                if final or response.stop_reason != "tool_use" or not tool_uses:
                    break
                turn.messages.append({"role": "user", "content": await _run_tools(turn, tool_uses, emit)})
        if not turn.answer_text().strip() and not turn.tables and not turn.charts and not turn.chips \
                and not turn.places and not turn.decisions:
            raise ProviderError("anthropic", "The assistant returned no answer; try again", error_code="AI_INCOMPLETE")
        # The header's note digest is not a source either (memory.without_note_texts)
        user_texts = [memory.without_note_texts(text) for text in
                      (user_content, *(m["content"] for m in turn.history.messages
                                       if m["role"] == "user" and isinstance(m["content"], str)))]
        app = [r["data"] for r in turn.all_results().values() if r["tool"] == FIND_IN_APP]
        ungrounded = ungrounded_numbers(
            turn.answer_text(),
            # A number in a crawled screen is sample data, and one in a note is the
            # agent's own earlier words: neither is a source for the user's stats
            [r["data"] for r in turn.all_results().values() if r["tool"] not in NOT_SOURCES]
            + [r["input"] for r in turn.results.values() if r["tool"] not in NOT_SOURCES]
            + user_texts,
            turn.run.names,
        )
        made_up = made_up_places(turn.answer_text(), app,
                                 [r["data"] for r in turn.all_results().values() if r["tool"] != FIND_IN_APP]
                                 + user_texts)
        places = len(made_up)
        outcome = "ok"
    except TimeoutError:
        outcome = "AI_TIMEOUT"
        emit("error", {"code": outcome, "message": "The assistant took too long; try again"})
    except AppError as exc:
        outcome = exc.error_code
        emit("error", {"code": outcome, "message": exc.message})
    except Exception:
        get_logger().exception("ai_turn_failed", conversation_id=turn.conversation_id)
        emit("error", {"code": outcome, "message": "Something went wrong; try again"})
    finally:
        duration_ms = round((time.monotonic() - started) * 1000)
        try:
            turn_id = await conversations.record(
                turn.conversation_id,
                {
                    "user_id": turn.user_id,
                    "message": turn.message,
                    # Only a finished turn becomes history: a half-done one would
                    # end on an unanswered tool call, or on a refused answer
                    "messages": turn.messages if outcome == "ok" else None,
                    "results": turn.results if outcome == "ok" else {},
                    "parts": turn.parts,
                    "tool_calls": [c.model_dump() for c in turn.run.tool_calls],
                    "outcome": outcome,
                    "model": turn.run.model or None,
                    "model_calls": turn.run.model_calls,
                    "input_tokens": turn.run.input_tokens,
                    "output_tokens": turn.run.output_tokens,
                    "cache_read_input_tokens": turn.run.cache_read_input_tokens,
                    "cache_creation_input_tokens": turn.run.cache_creation_input_tokens,
                    "context_tokens": turn.context_tokens,
                    "cost_micros": turn.cost,
                    "duration_ms": duration_ms,
                    "ungrounded_numbers": ungrounded,
                },
                title=conversations.title_of(turn.message),
                reservation_id=turn.reservation_id,
                proposal_ids=turn.proposals,
            )
        except Exception:
            # The spend is real even when the row isn't: settle it on its own (or,
            # failing that, it stays charged at the ceiling), and release the
            # conversation so the user can go on
            get_logger().exception("ai_turn_record_failed", conversation_id=turn.conversation_id, cost_micros=turn.cost)
            await _settle_quietly(turn)
            await _release_quietly(turn.conversation_id)
        _log_turn(turn, outcome, duration_ms, ungrounded, turn_id, places, made_up=made_up)
        try:
            month = await spend.month_spend(turn.user_id)
            spent = {"month_usd": month.user / 1e6, "cap_usd": month.user_cap / 1e6}
        except Exception:
            spent = None
        emit("done", {
            "turn_id": turn_id,
            "outcome": outcome,
            "cost_usd": turn.cost / 1e6,
            "model_calls": turn.run.model_calls,
            "duration_ms": duration_ms,
            "spend": spent,
        })
        emit.close()
    return TurnResult(turn_id=turn_id, outcome=outcome, cost=turn.cost)


async def _settle_quietly(turn: Turn) -> None:
    if turn.reservation_id is None:
        return
    try:
        await spend.settle(turn.reservation_id, turn.cost)
    except Exception:
        get_logger().exception("ai_spend_settle_failed", reservation_id=turn.reservation_id, cost_micros=turn.cost)


async def _release_quietly(conversation_id: int) -> None:
    try:
        await conversations.release(conversation_id)
    except Exception:
        get_logger().exception("ai_conversation_release_failed", conversation_id=conversation_id)


# The made-up places a turn's log line names, so a count can be read without the answer
_LOGGED_PLACES = 5


def _log_turn(turn: Turn, outcome: str, duration_ms: int, ungrounded: Optional[int], turn_id: Optional[int],
              places: Optional[int] = None, *, made_up: Optional[list[str]] = None) -> None:
    run = turn.run
    get_logger().info(
        "ai_turn",
        conversation_id=turn.conversation_id,
        turn_id=turn_id,
        user_id=turn.user_id,
        outcome=outcome,
        model=run.model or settings.ai_agent_model,
        effort=settings.ai_agent_effort,
        model_calls=run.model_calls,
        tool_calls=[call.name for call in run.tool_calls],
        tool_errors=sum(call.is_error for call in run.tool_calls),
        tables=turn.tables,
        charts=turn.charts,
        places=turn.places,
        decisions=turn.decisions,
        checkin_id=turn.checkin_id,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_input_tokens=run.cache_read_input_tokens,
        cache_creation_input_tokens=run.cache_creation_input_tokens,
        context_tokens=turn.context_tokens,
        cost_micros=turn.cost,
        fallback=run.fallback,
        declined_attempts_billed=run.declined_attempts_billed,
        stop_reason=run.stop_reason,
        duration_ms=duration_ms,
        ungrounded_numbers=ungrounded,
        ungrounded_places=places,
        made_up_places=(made_up or [])[:_LOGGED_PLACES],
        prior_turns=turn.history.turns,
        request_ids=run.request_ids,
    )


# Turns still running after their stream closed; held so they aren't collected
_RUNNING: set[asyncio.Task] = set()


def start(turn: Turn, header_line: str) -> Emitter:
    """Run the turn in its own task and return the stream of its events."""
    emit = Emitter()
    task = asyncio.create_task(run_turn(turn, emit, header_line=header_line))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return emit
