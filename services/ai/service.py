"""
The bounded tool loop behind POST /v1/internal/ai/ask and /ai/route.

A hand-written loop rather than the SDK's beta tool runner, because the
bounds are the point: at most `ai_max_model_calls` model calls, the last one
made with tools disabled so the request always ends in an answer, and at most
`ai_max_tool_calls` tool executions across all of them. Every request logs one
`ai_request` line with its token spend whether it succeeds or not; a failed
request can still have cost money.

Refusals: Claude Opus 5's safety classifiers can decline a request. The
server-side `fallbacks="default"` option re-serves a declined request on
Anthropic's recommended fallback model inside the same call. A refusal that
still comes back means the whole chain declined.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from core.errors import AppError, ProviderError, ProviderTimeout, ServiceUnavailableError
from core.logging import get_logger
from core.nba_calendar import nba_date_et
from core.season import previous_season
from core.settings import settings
from schemas.ai import AiContext, AiToolCall, AiUsage, AskData, AskResp, RouteData, RouteResp
from schemas.common import ApiStatus
from services.ai import guards, questions, routing
from services.ai.client import get_client
from services.ai.prompts import ROUTER_PROMPT, SYSTEM_PROMPT
from services.ai.tools import ASK_TOOL_NAMES, ROUTER_TOOL_NAMES, ROUTER_TOOLS, TOOLS, ToolContext, run_tool
from services.schedule_service import get_season_bounds

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000  # a backstop the model never sees; answer length is set by the prompt
BUDGET_SPENT = "Not run: this request's lookup budget is used up. Answer with what you have."


@dataclass
class _Run:
    """What one request spent, accumulated across its model calls."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    fallback: bool = False
    declined_attempts_billed: int = 0
    model: str = ""
    stop_reason: str | None = None
    request_ids: list[str] = field(default_factory=list)
    tool_calls: list[AiToolCall] = field(default_factory=list)
    # Player, team and league names the lookups and the view supplied (for the number check)
    names: set[str] = field(default_factory=set)

    def record(self, response: Any) -> None:
        usage = response.usage
        self.model_calls += 1
        self._add(usage)
        # Top-level usage covers only the attempt that served the message. When a
        # fallback served it (a fallback_message iteration), the attempts that
        # declined are `message` iterations: one declined before any output is
        # not billed, one declined mid-output is -- so count those too, or the
        # meters come up short on exactly the requests that cost extra.
        entries = usage.iterations or []
        if any(getattr(entry, "type", None) == "fallback_message" for entry in entries):
            self.fallback = True
            for entry in entries:
                if getattr(entry, "type", None) == "message" and (getattr(entry, "output_tokens", 0) or 0) > 0:
                    self._add(entry)
                    self.declined_attempts_billed += 1
        self.model = response.model
        self.stop_reason = response.stop_reason
        request_id = getattr(response, "_request_id", None)
        if request_id:
            self.request_ids.append(request_id)

    def _add(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def usage(self) -> AiUsage:
        return AiUsage(
            model=self.model or settings.ai_model,
            model_calls=self.model_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            fallback=self.fallback,
        )


def _served_content(content: list[Any]) -> list[Any]:
    """The blocks to carry forward from a response.

    A `fallback` block marks where a declining model handed over. Blocks before
    the last marker belong to the model that declined: its text is continuation
    context and stays, anything else (thinking, tool calls) goes. The markers
    themselves are audit-only.
    """
    last = max((i for i, block in enumerate(content) if block.type == "fallback"), default=-1)
    return [
        block
        for i, block in enumerate(content)
        if block.type != "fallback" and (i > last or block.type == "text")
    ]


async def _create(**kwargs: Any) -> Any:
    """One Messages API call, with the SDK's errors mapped onto the app's taxonomy.

    Most specific first: every status error subclasses APIStatusError, and
    APITimeoutError subclasses APIConnectionError.
    """
    try:
        return await get_client().beta.messages.create(**kwargs)
    except anthropic.APITimeoutError as exc:
        raise ProviderTimeout("anthropic", "The assistant timed out; try again", error_code="AI_TIMEOUT") from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError("anthropic", "The assistant is unreachable; try again", error_code="AI_UNAVAILABLE") from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise ServiceUnavailableError("AI_MISCONFIGURED", "The assistant is unavailable") from exc
    except anthropic.RateLimitError as exc:
        raise ProviderError("anthropic", "The assistant is busy; try again in a minute", error_code="AI_BUSY") from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise ProviderError("anthropic", "The assistant is unavailable; try again", error_code="AI_UNAVAILABLE") from exc
        # 400/404/413: a request we built wrong is a bug on our side, not an outage
        raise AppError("AI_REQUEST_REJECTED", "The assistant couldn't process that question", status_code=500) from exc


async def _run_tools(
    tool_uses: list[Any],
    run: _Run,
    *,
    ctx: ToolContext | None,
    allowed: frozenset[str],
) -> list[dict[str, Any]]:
    """Execute this turn's tool calls within the request's remaining budget.

    Every tool_use gets a tool_result -- over-budget calls get a refusal the
    model can read -- and all results go back in one user message.
    """
    budget = max(settings.ai_max_tool_calls - len(run.tool_calls), 0)
    runnable, over_budget = tool_uses[:budget], tool_uses[budget:]
    outcomes = await asyncio.gather(*(
        run_tool(block.name, block.input, ctx=ctx, allowed=allowed) for block in runnable
    ))

    results: list[dict[str, Any]] = []
    for block, outcome in zip(runnable, outcomes):
        if not outcome.is_error:
            run.names |= _names_in(outcome.content)
        run.tool_calls.append(AiToolCall(name=block.name, input=_as_dict(block.input), is_error=outcome.is_error))
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": outcome.content,
                        "is_error": outcome.is_error})
    for block in over_budget:
        run.tool_calls.append(AiToolCall(name=block.name, input=_as_dict(block.input), is_error=True))
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": BUDGET_SPENT, "is_error": True})
    return results


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


_NAME_KEYS = ("name", "team_name", "league_name")


def _names_in(content: str) -> set[str]:
    """Every name-like string value in a tool result's JSON, at any depth."""
    try:
        data = json.loads(content)
    except ValueError:
        return set()
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _NAME_KEYS and isinstance(value, str):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(data)
    return found


async def _loop(
    user_turn: str,
    run: _Run,
    *,
    system: str,
    tools: list[dict[str, Any]],
    allowed: frozenset[str],
    ctx: ToolContext | None = None,
    output_format: dict[str, Any] | None = None,
) -> Any:
    """Run the bounded loop and return the final response.

    `output_format` goes on every call, not just the last: it is part of the
    request, so changing it between calls would change the cached prefix.
    """
    output_config: dict[str, Any] = {"effort": settings.ai_effort}
    if output_format is not None:
        output_config["format"] = output_format
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_turn}]
    response: Any = None
    for call in range(1, settings.ai_max_model_calls + 1):
        final = call == settings.ai_max_model_calls or len(run.tool_calls) >= settings.ai_max_tool_calls
        response = await _create(
            model=settings.ai_model,
            max_tokens=MAX_TOKENS,
            # An explicit breakpoint on the fixed prefix (tools + system) lets
            # requests share it. The top-level breakpoint below sits after each
            # request's own question, so on its own no two requests ever match.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config=output_config,
            # Calls within one request are seconds apart, so each re-reads the last one's prefix
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **({"tool_choice": {"type": "none"}} if final else {}),
        )
        run.record(response)

        if response.stop_reason == "refusal":
            raise AppError("AI_DECLINED", "The assistant can't help with that question",
                           status_code=422, log_level="warning")
        if response.stop_reason == "max_tokens":
            raise ProviderError("anthropic", "The assistant's answer was cut off; try a narrower question",
                                error_code="AI_INCOMPLETE")

        content = _served_content(response.content)
        tool_uses = [block for block in content if block.type == "tool_use"]
        if final or response.stop_reason != "tool_use" or not tool_uses:
            break
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": await _run_tools(tool_uses, run, ctx=ctx, allowed=allowed)})
    return response


def _final_text(response: Any) -> str:
    text = "".join(block.text for block in _served_content(response.content) if block.type == "text").strip()
    if not text:
        raise ProviderError("anthropic", "The assistant returned no answer; try again", error_code="AI_INCOMPLETE")
    return text


async def _answer(question: str, run: _Run) -> str:
    response = await _loop(question, run, system=SYSTEM_PROMPT, tools=TOOLS, allowed=ASK_TOOL_NAMES)
    return _final_text(response)


def _router_turn(question: str, view: dict[str, Any], season: str) -> str:
    """The user turn: the season, the current view, then the question. Kept out of the
    system prompt so the cached prefix is the same for everyone."""
    return f"{season}\nCurrent view: {json.dumps(view, sort_keys=True)}\n\nQuestion: {question}"


def _season_line_sync() -> str:
    """Which season "this season" means today. Before opening night it's last
    season's games that exist -- StatMuse and our own views both answer with those."""
    current = settings.nba_season
    try:
        opening = get_season_bounds().opening_night
    except Exception:
        return f"NBA season: {current}."
    if nba_date_et() < opening:
        return (f"NBA season: {current} starts {opening.isoformat()}; "
                f"the latest season with games is {previous_season(current)}.")
    return f"NBA season: {current}, in progress."


async def _season_line() -> str:
    # The calendar is a cached file read, but the first one still touches disk
    return await asyncio.to_thread(_season_line_sync)


def _log_request(endpoint: str, user_id: int, outcome: str, run: _Run, started: float, **extra: Any) -> None:
    get_logger().info(
        "ai_request",
        endpoint=endpoint,
        user_id=user_id,
        outcome=outcome,
        model=run.model or settings.ai_model,
        effort=settings.ai_effort,
        model_calls=run.model_calls,
        tool_calls=[call.name for call in run.tool_calls],
        tool_errors=sum(call.is_error for call in run.tool_calls),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_input_tokens=run.cache_read_input_tokens,
        cache_creation_input_tokens=run.cache_creation_input_tokens,
        fallback=run.fallback,
        declined_attempts_billed=run.declined_attempts_billed,
        stop_reason=run.stop_reason,
        duration_ms=round((time.monotonic() - started) * 1000),
        request_ids=run.request_ids,
        **extra,
    )


class AiService:

    @staticmethod
    async def ask(question: str, *, user_id: int) -> AskResp:
        run = _Run()
        started = time.monotonic()
        outcome = "INTERNAL_ERROR"
        try:
            # Inside the logging scope: a request turned away by the kill switch
            # or by a quota is still a request, and quota exhaustion is only
            # visible in telemetry if the rejection is logged too.
            guards.ensure_enabled()
            await guards.consume_quota(user_id)
            async with asyncio.timeout(settings.ai_request_timeout_seconds):
                answer = await _answer(question, run)
            outcome = "ok"
        except TimeoutError as exc:
            outcome = "AI_TIMEOUT"
            raise ProviderTimeout("anthropic", "The assistant took too long; try again", error_code=outcome) from exc
        except AppError as exc:
            outcome = exc.error_code
            raise
        finally:
            _log_request("ask", user_id, outcome, run, started)

        return AskResp(
            status=ApiStatus.SUCCESS,
            message="Answered",
            data=AskData(answer=answer, tool_calls=run.tool_calls, usage=run.usage()),
        )

    @staticmethod
    async def route(question: str, context: AiContext, *, user_id: int) -> RouteResp:
        """Take a question to the place that answers it (docs/AI_PHASE1_PLAN.md)."""
        run = _Run()
        started = time.monotonic()
        outcome = "INTERNAL_ERROR"
        reached_model = False
        answer: routing.RouterAnswer | None = None
        ungrounded: int | None = None
        question_id: int | None = None
        try:
            guards.ensure_enabled()
            await guards.consume_quota(user_id)
            reached_model = True
            async with asyncio.timeout(settings.ai_request_timeout_seconds):
                view = await routing.describe_view(context)
                named = [view.get("player"), *view.get("compare", []), view.get("nba_team")]
                run.names |= {n["name"] for n in named if n and n.get("name")}
                response = await _loop(
                    _router_turn(question, view, await _season_line()),
                    run,
                    system=ROUTER_PROMPT,
                    tools=ROUTER_TOOLS,
                    allowed=ROUTER_TOOL_NAMES,
                    ctx=ToolContext(user_id=user_id),
                    output_format=routing.ANSWER_FORMAT,
                )
                answer = await routing.validate(
                    routing.parse_answer(_final_text(response)), user_id=user_id, names=run.names)
            ungrounded = routing.ungrounded_numbers(answer.text, question, answer.target, run.names)
            outcome = "ok"
        except TimeoutError as exc:
            outcome = "AI_TIMEOUT"
            raise ProviderTimeout("anthropic", "The assistant took too long; try again", error_code=outcome) from exc
        except AppError as exc:
            outcome = exc.error_code
            raise
        finally:
            # Every question that reached the model is logged -- a failed one too,
            # since a question the router chokes on is exactly what the review wants.
            if reached_model:
                question_id = await questions.record(
                    user_id=user_id,
                    question=question,
                    context=context.model_dump(exclude_none=True, exclude_defaults=True),
                    kind=answer.kind if answer else None,
                    # A refused target is kept for the review; the client never sees the row
                    target=(answer.target.model_dump() if answer.target else answer.rejected_target) if answer else None,
                    statmuse_query=answer.statmuse_query if answer else None,
                    gap=answer.gap if answer else None,
                    missing=answer.missing if answer else None,
                    tool_calls=[call.model_dump() for call in run.tool_calls],
                    outcome=outcome,
                    model_calls=run.model_calls,
                    input_tokens=run.input_tokens,
                    output_tokens=run.output_tokens,
                    cache_read_input_tokens=run.cache_read_input_tokens,
                    cache_creation_input_tokens=run.cache_creation_input_tokens,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    ungrounded_numbers=ungrounded,
                )
            _log_request(
                "route", user_id, outcome, run, started,
                kind=answer.kind if answer else None,
                gap=answer.gap if answer else None,
                ungrounded_numbers=ungrounded,
                question_id=question_id,
            )

        return RouteResp(
            status=ApiStatus.SUCCESS,
            message="Routed",
            data=RouteData(
                kind=answer.kind,
                text=answer.text,
                target=answer.target,
                statmuse_query=answer.statmuse_query,
                statmuse_url=routing.statmuse_url(answer.statmuse_query) if answer.statmuse_query else None,
                suggestions=answer.suggestions,
                gap=answer.gap,
                question_id=question_id,
                sources=run.tool_calls,
                usage=run.usage(),
            ),
        )
