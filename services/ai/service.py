"""
The bounded tool loop behind POST /v1/internal/ai/ask.

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
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from core.errors import AppError, ProviderError, ProviderTimeout, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from schemas.ai import AiToolCall, AiUsage, AskData, AskResp
from schemas.common import ApiStatus
from services.ai import guards
from services.ai.client import get_client
from services.ai.prompts import SYSTEM_PROMPT
from services.ai.tools import TOOLS, run_tool

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
    model: str = ""
    stop_reason: str | None = None
    request_ids: list[str] = field(default_factory=list)
    tool_calls: list[AiToolCall] = field(default_factory=list)

    def record(self, response: Any) -> None:
        usage = response.usage
        self.model_calls += 1
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_input_tokens += usage.cache_read_input_tokens or 0
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens or 0
        # The served-by signal: a fallback_message iteration means a fallback model ran
        self.fallback = self.fallback or any(
            getattr(entry, "type", None) == "fallback_message" for entry in (usage.iterations or [])
        )
        self.model = response.model
        self.stop_reason = response.stop_reason
        request_id = getattr(response, "_request_id", None)
        if request_id:
            self.request_ids.append(request_id)

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
        raise ProviderTimeout("anthropic", "The assistant timed out; try again") from exc
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


async def _run_tools(tool_uses: list[Any], run: _Run) -> list[dict[str, Any]]:
    """Execute this turn's tool calls within the request's remaining budget.

    Every tool_use gets a tool_result -- over-budget calls get a refusal the
    model can read -- and all results go back in one user message.
    """
    budget = max(settings.ai_max_tool_calls - len(run.tool_calls), 0)
    allowed, over_budget = tool_uses[:budget], tool_uses[budget:]
    outcomes = await asyncio.gather(*(run_tool(block.name, block.input) for block in allowed))

    results: list[dict[str, Any]] = []
    for block, outcome in zip(allowed, outcomes):
        run.tool_calls.append(AiToolCall(name=block.name, input=_as_dict(block.input), is_error=outcome.is_error))
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": outcome.content,
                        "is_error": outcome.is_error})
    for block in over_budget:
        run.tool_calls.append(AiToolCall(name=block.name, input=_as_dict(block.input), is_error=True))
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": BUDGET_SPENT, "is_error": True})
    return results


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def _answer(question: str, run: _Run) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    response: Any = None
    for call in range(1, settings.ai_max_model_calls + 1):
        final = call == settings.ai_max_model_calls or len(run.tool_calls) >= settings.ai_max_tool_calls
        response = await _create(
            model=settings.ai_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": settings.ai_effort},
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
        messages.append({"role": "user", "content": await _run_tools(tool_uses, run)})

    answer = "".join(block.text for block in _served_content(response.content) if block.type == "text").strip()
    if not answer:
        raise ProviderError("anthropic", "The assistant returned no answer; try again", error_code="AI_INCOMPLETE")
    return answer


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
            raise ProviderTimeout("anthropic", "The assistant took too long; try again") from exc
        except AppError as exc:
            outcome = exc.error_code
            raise
        finally:
            get_logger().info(
                "ai_request",
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
                stop_reason=run.stop_reason,
                duration_ms=round((time.monotonic() - started) * 1000),
                request_ids=run.request_ids,
            )

        return AskResp(
            status=ApiStatus.SUCCESS,
            message="Answered",
            data=AskData(answer=answer, tool_calls=run.tool_calls, usage=run.usage()),
        )
