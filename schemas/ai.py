"""
Schemas for the AI layer (docs/AI_LAYER_PLAN.md).

The answer is prose, but everything around it is structured on purpose: the
tool calls say which Court Vision lookups the answer rests on, and the usage
block makes each request's token spend visible to the caller as well as to
the `ai_request` log line.
"""

from typing import Any, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse


class AskReq(ApiModel):
    """Body for POST /v1/internal/ai/ask."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="A question about NBA players, in plain language",
    )


class AiToolCall(ApiModel):
    """One Court Vision lookup the model made while answering."""

    name: str = Field(..., description="Tool name, e.g. get_player_stats")
    input: dict[str, Any] = Field(..., description="Arguments the model passed")
    is_error: bool = Field(..., description="True when the lookup failed or was refused")


class AiUsage(ApiModel):
    """Token spend for one request, summed over its model calls."""

    model: str = Field(..., description="Model that produced the final answer")
    model_calls: int = Field(..., description="Messages API calls made")
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    fallback: bool = Field(..., description="True when a refusal was re-served by a fallback model")


class AskData(ApiModel):
    answer: str = Field(..., description="The model's answer")
    tool_calls: list[AiToolCall] = Field(default_factory=list)
    usage: AiUsage


class AskResp(BaseResponse):
    """Response for POST /v1/internal/ai/ask."""

    data: Optional[AskData] = None
