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
  model's progress notes become status lines; display tools become blocks once
  the server has built them. Events go on a queue the route drains as SSE.
- **Detached.** The turn runs in its own task. A client that disconnects stops
  hearing it, not running it: the tokens are already being spent, and the turn
  must still be written so the conversation stays whole.

Every turn writes one usr.ai_turns row, failed or not (a failed turn can
still have cost money), and logs one `ai_turn` line.
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
from schemas.ai import AiToolCall
from services.ai import conversations, spend
from services.ai.client import get_client
from services.ai.display import DISPLAY_TOOL_NAMES, SHOW_TABLE, DisplayError, show_table
from services.ai.prompts import AGENT_PROMPT
from services.ai.service import FALLBACK_BETA, _names_in, _Run, _season_line, _served_content, map_sdk_error
from services.ai.tools import AGENT_TOOL_NAMES, AGENT_TOOLS, ToolContext, list_my_teams, run_tool

# Progress notes between tool calls come back as `thinking` blocks on Opus 5.5;
# "updates" returns a short summary of each one and keeps the reasoning hidden.
UPDATES_BETA = "thinking-display-updates-2026-08-18"
MAX_TOKENS = 32_000  # per call: thinking counts toward it, and a cut-off answer is a failed turn
BUDGET_SPENT = "Not run: this turn's lookup budget is used up. Answer with what you have."
TOOLS: list[dict[str, Any]] = [*AGENT_TOOLS, SHOW_TABLE]
ALLOWED = AGENT_TOOL_NAMES

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
    "show_table": "Building a table",
}


@dataclass
class Turn:
    """One turn's state: what it appended, what it showed, what it found."""

    conversation_id: int
    user_id: int
    message: str
    history: conversations.History
    # The spend reserved for this turn (usr.ai_spend), settled when it is written
    reservation_id: Optional[int] = None
    run: _Run = field(default_factory=_Run)
    messages: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    parts: list[dict[str, Any]] = field(default_factory=list)
    cost: int = 0
    context_tokens: int = 0
    tables: int = 0

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

def _team_facts(teams: list[dict[str, Any]], pinned: Optional[int]) -> str:
    if not teams:
        return "The user has no teams yet."
    facts = [f"team_id {t['team_id']} ({t['provider']}, {t['scoring'] or 'unknown'} scoring, {t['season']})"
             for t in teams]
    line = "The user's teams: " + "; ".join(facts) + "."
    if pinned is not None and any(t["team_id"] == pinned for t in teams):
        line += f" Pinned team: team_id {pinned}."
    return line


async def header(user_id: int, pinned: Optional[int], now: Optional[datetime] = None) -> str:
    """Today, the season, and the user's teams, by ID and format only: names are
    league data, and the model can read them with get_my_teams."""
    today = nba_date_et() if now is None else now.astimezone(EASTERN).date()
    try:
        teams = await list_my_teams(user_id)
    except Exception:
        get_logger().warning("ai_turn_header_teams_failed", user_id=user_id)
        teams = []
    season = await _season_line()
    return f"[Today is {today.strftime('%A')} {today.isoformat()} (US Eastern). {season} {_team_facts(teams, pinned)}]"


# ---- one model call, streamed ----

async def _stream_call(turn: Turn, emit: Emitter, *, final: bool, messages: list[dict[str, Any]]) -> Any:
    notes: dict[int, str] = {}
    try:
        async with get_client().beta.messages.stream(
            model=settings.ai_agent_model,
            max_tokens=MAX_TOKENS,
            # The fixed prefix (tools + system) gets its own breakpoint, shared by
            # every conversation; the top-level one caches each conversation so far.
            system=[{"type": "text", "text": AGENT_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive", "display": "updates"},
            output_config={"effort": settings.ai_agent_effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA, UPDATES_BETA],
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
            shown = show_table(raw, turn.all_results(), f"t{turn.tables + 1}")
        except DisplayError as exc:
            return {"type": "tool_result", "tool_use_id": block.id, "content": str(exc), "is_error": True}
        turn.tables += 1
        turn.parts.append({"kind": "block", "block": shown.block})
        emit("block", shown.block)
        return {"type": "tool_result", "tool_use_id": block.id, "content": shown.summary}

    outcome = await run_tool(name, raw, ctx=ToolContext(user_id=turn.user_id), allowed=ALLOWED)
    if outcome.is_error:
        return {"type": "tool_result", "tool_use_id": block.id, "content": outcome.content, "is_error": True}
    result_id = turn.next_result_id()
    data = json.loads(outcome.content)
    turn.results[result_id] = {"tool": name, "input": raw if isinstance(raw, dict) else {}, "data": data}
    turn.run.names |= _names_in(outcome.content)
    return {"type": "tool_result", "tool_use_id": block.id,
            "content": json.dumps({"result_id": result_id, **data}, default=str, ensure_ascii=False)}


async def _run_tools(turn: Turn, tool_uses: list[Any], emit: Emitter) -> list[dict[str, Any]]:
    """Every tool_use gets a tool_result, all in one user message. Lookups run
    together; display calls run after them, in the order the model made them."""
    budget = max(settings.ai_agent_max_tool_calls - len(turn.run.tool_calls), 0)
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


# ---- the turn ----

async def run_turn(turn: Turn, emit: Emitter, *, header_line: str) -> None:
    """Run one turn to the end, write it, and close the stream. Never raises:
    failures become an `error` event and the turn's outcome."""
    started = time.monotonic()
    outcome = "INTERNAL_ERROR"
    turn_id: Optional[int] = None
    ungrounded: Optional[int] = None
    user_content = f"{header_line}\n\n{turn.message}"
    turn.messages.append({"role": "user", "content": user_content})
    try:
        async with asyncio.timeout(settings.ai_agent_turn_timeout_seconds):
            for call in range(1, settings.ai_agent_max_model_calls + 1):
                final = (call == settings.ai_agent_max_model_calls
                         or len(turn.run.tool_calls) >= settings.ai_agent_max_tool_calls
                         or turn.cost >= round(settings.ai_agent_turn_max_usd * 1_000_000))
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
        if not turn.answer_text().strip() and not turn.tables:
            raise ProviderError("anthropic", "The assistant returned no answer; try again", error_code="AI_INCOMPLETE")
        ungrounded = ungrounded_numbers(
            turn.answer_text(),
            [r["data"] for r in turn.all_results().values()]
            + [r["input"] for r in turn.results.values()]
            + [user_content, *(m["content"] for m in turn.history.messages
                               if m["role"] == "user" and isinstance(m["content"], str))],
            turn.run.names,
        )
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
            )
        except Exception:
            # The spend is real even when the row isn't: settle it on its own (or,
            # failing that, it stays charged at the ceiling), and release the
            # conversation so the user can go on
            get_logger().exception("ai_turn_record_failed", conversation_id=turn.conversation_id, cost_micros=turn.cost)
            await _settle_quietly(turn)
            await _release_quietly(turn.conversation_id)
        _log_turn(turn, outcome, duration_ms, ungrounded, turn_id)
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


def _log_turn(turn: Turn, outcome: str, duration_ms: int, ungrounded: Optional[int], turn_id: Optional[int]) -> None:
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
