"""The AI layer's routes (docs/AI_LAYER_PLAN.md, docs/AI_AGENT_PLAN.md). Off unless AI_ENABLED is set."""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import StreamingResponse

from api.deps import UserContext, get_db_user
from core.errors import NotFoundError
from schemas.ai import (
    AiConversationCreateReq,
    AiConversationListResp,
    AiConversationResp,
    AiFeedbackData,
    AiFeedbackReq,
    AiFeedbackResp,
    AiTurnCommentData,
    AiTurnCommentReq,
    AiTurnCommentResp,
    AiTurnFeedbackData,
    AiTurnFeedbackResp,
    AiTurnReq,
    AiUsageResp,
    AskReq,
    AskResp,
    RouteReq,
    RouteResp,
)
from schemas.common import ApiStatus, BaseResponse
from services.ai import questions
from services.ai.agent import Emitter
from services.ai.agent_service import AgentService
from services.ai.service import AiService

router = APIRouter(prefix="/ai", tags=["AI"])


@router.post(
    "/ask",
    response_model=AskResp,
    summary="Ask the assistant a question about NBA players",
    description=(
        "Answers by looking players up through Court Vision's own services; every number "
        "in the answer comes from one of those lookups, listed in `tool_calls`. Each caller "
        "has a daily allowance of questions, and the route is off unless AI_ENABLED is set."
    ),
    responses={
        422: {"description": "Invalid question, or the assistant declined it (AI_DECLINED)"},
        429: {"description": "The caller's daily allowance is used up (AI_QUOTA_EXCEEDED)"},
        502: {"description": "The model provider failed (AI_UNAVAILABLE, AI_BUSY, AI_INCOMPLETE)"},
        503: {"description": "Turned off (AI_DISABLED) or today's global budget is spent (AI_DAILY_BUDGET_REACHED)"},
        504: {"description": "The assistant took too long (AI_TIMEOUT)"},
    },
)
async def ask(req: AskReq, user: UserContext = Depends(get_db_user)) -> AskResp:
    return await AiService.ask(req.question, user_id=user.user_id)


@router.post(
    "/route",
    response_model=RouteResp,
    summary="Route a question to the place that answers it",
    description=(
        "Returns one of three kinds. `show`: a Court Vision view, as a terminal or page `target` "
        "the client applies. `statmuse`: an NBA stat question no view covers, with a server-built "
        "`statmuse_url` the user opens. `cannot`: neither covers it, with `suggestions`. Every ID "
        "in a target has been checked, and a fantasy team is always one of the caller's. Shares "
        "the daily allowance with /ai/ask and is off unless AI_ENABLED is set."
    ),
    responses={
        422: {"description": "Invalid question or context, or the assistant declined it (AI_DECLINED)"},
        429: {"description": "The caller's daily allowance is used up (AI_QUOTA_EXCEEDED)"},
        502: {"description": "The model provider failed (AI_UNAVAILABLE, AI_BUSY, AI_INCOMPLETE)"},
        503: {"description": "Turned off (AI_DISABLED) or today's global budget is spent (AI_DAILY_BUDGET_REACHED)"},
        504: {"description": "The assistant took too long (AI_TIMEOUT)"},
    },
)
async def route(req: RouteReq, user: UserContext = Depends(get_db_user)) -> RouteResp:
    return await AiService.route(req.question, req.context, user_id=user.user_id)


@router.post(
    "/questions/{question_id}/feedback",
    response_model=AiFeedbackResp,
    summary="Rate a routed answer",
    description="Thumbs up or down on one of the caller's own routed questions; null clears it.",
    responses={404: {"description": "No such question for this caller (AI_QUESTION_NOT_FOUND)"}},
)
async def question_feedback(
    question_id: int = Path(..., gt=0),
    req: AiFeedbackReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiFeedbackResp:
    if not await questions.set_feedback(question_id, user.user_id, req.feedback):
        raise NotFoundError("AI_QUESTION_NOT_FOUND", "Question not found")
    return AiFeedbackResp(
        status=ApiStatus.SUCCESS,
        message="Feedback saved",
        data=AiFeedbackData(question_id=question_id, feedback=req.feedback),
    )


# ---- The agent: the AI desk (docs/AI_AGENT_PLAN.md). Invite-only. ----

_AGENT_ERRORS = {
    403: {"description": "The caller is not on the invite list (AI_INVITE_ONLY)"},
    503: {"description": "Turned off (AI_DISABLED)"},
}
# Silence longer than this gets a comment line, so proxies keep the stream open
# while the model thinks.
KEEPALIVE_SECONDS = 15.0


@router.get(
    "/usage",
    response_model=AiUsageResp,
    summary="The caller's access to the agent and this month's spend",
    description="Open to any signed-in user, so the desk can say whether the agent is available to them.",
    responses={503: _AGENT_ERRORS[503]},
)
async def agent_usage(user: UserContext = Depends(get_db_user)) -> AiUsageResp:
    return AiUsageResp(status=ApiStatus.SUCCESS, message="Usage", data=await AgentService.usage(user.user_id))


@router.post(
    "/conversations",
    response_model=AiConversationResp,
    summary="Start a conversation with the agent",
    responses={**_AGENT_ERRORS, 404: {"description": "team_id is not one of the caller's teams (TEAM_NOT_FOUND)"}},
)
async def create_conversation(
    req: AiConversationCreateReq = Body(default_factory=AiConversationCreateReq),
    user: UserContext = Depends(get_db_user),
) -> AiConversationResp:
    data = await AgentService.create(user.user_id, req.team_id)
    return AiConversationResp(status=ApiStatus.SUCCESS, message="Conversation started", data=data)


@router.get(
    "/conversations",
    response_model=AiConversationListResp,
    summary="The caller's conversations, most recent first",
    responses=_AGENT_ERRORS,
)
async def list_conversations(user: UserContext = Depends(get_db_user)) -> AiConversationListResp:
    return AiConversationListResp(status=ApiStatus.SUCCESS, message="Conversations",
                                  data=await AgentService.list(user.user_id))


@router.get(
    "/conversations/{conversation_id}",
    response_model=AiConversationResp,
    summary="One conversation with every turn",
    responses={**_AGENT_ERRORS, 404: {"description": "No such conversation for this caller"}},
)
async def get_conversation(
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiConversationResp:
    data = await AgentService.detail(conversation_id, user.user_id)
    return AiConversationResp(status=ApiStatus.SUCCESS, message="Conversation", data=data)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=BaseResponse,
    summary="Delete a conversation and its turns",
    responses={**_AGENT_ERRORS, 404: {"description": "No such conversation for this caller"}},
)
async def delete_conversation(
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> BaseResponse:
    await AgentService.delete(conversation_id, user.user_id)
    return BaseResponse(status=ApiStatus.SUCCESS, message="Conversation deleted")


async def _events(emit: Emitter) -> AsyncIterator[str]:
    """Server-sent events until the turn closes the stream. A client that goes
    away only stops this generator; the turn finishes and is written anyway."""
    while True:
        try:
            item = await asyncio.wait_for(emit.queue.get(), KEEPALIVE_SECONDS)
        except TimeoutError:
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        event, data = item
        yield f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


@router.post(
    "/conversations/{conversation_id}/turns",
    summary="Send the agent a message; the answer streams back",
    description=(
        "A `text/event-stream` of the turn as it happens:\n\n"
        "- `status` `{text, kind, tool?}`: what the agent is doing (`kind` is `tool` for a lookup, "
        "`note` for the agent's own progress note);\n"
        "- `text` `{delta}`: the answer's prose, as it is written;\n"
        "- `block` (an `AiTable`): a table the server built from the agent's lookups;\n"
        "- `error` `{code, message}`: the turn failed (AI_DECLINED, AI_TIMEOUT, AI_INCOMPLETE, AI_BUSY, ...);\n"
        "- `done` `{turn_id, outcome, cost_usd, model_calls, duration_ms, spend}`: always last.\n\n"
        "Text and blocks arrive in the order the user should see them. Lines starting with `:` are "
        "keepalives. Errors before the stream starts are ordinary JSON errors."
    ),
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "The turn's events"},
        **_AGENT_ERRORS,
        404: {"description": "No such conversation for this caller (AI_CONVERSATION_NOT_FOUND)"},
        409: {"description": "Another turn is running (AI_CONVERSATION_BUSY), or the conversation "
                             "is full (AI_CONVERSATION_FULL)"},
        429: {"description": "The caller's monthly budget is used up (AI_USER_BUDGET_REACHED)"},
    },
)
async def send_turn(
    req: AiTurnReq,
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> StreamingResponse:
    emit = await AgentService.start_turn(conversation_id, user.user_id, req.message)
    return StreamingResponse(
        _events(emit),
        media_type="text/event-stream",
        # No caching, and no proxy buffering (nginx-style proxies honour X-Accel-Buffering)
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/turns/{turn_id}/feedback",
    response_model=AiTurnFeedbackResp,
    summary="Rate one of the agent's answers",
    description="Thumbs up or down on one of the caller's own turns; null clears it.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such turn for this caller (AI_TURN_NOT_FOUND)"}},
)
async def turn_feedback(
    turn_id: int = Path(..., gt=0),
    req: AiFeedbackReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiTurnFeedbackResp:
    await AgentService.feedback(turn_id, user.user_id, req.feedback)
    return AiTurnFeedbackResp(status=ApiStatus.SUCCESS, message="Feedback saved",
                              data=AiTurnFeedbackData(turn_id=turn_id, feedback=req.feedback))


@router.post(
    "/turns/{turn_id}/comment",
    response_model=AiTurnCommentResp,
    summary="Leave a note on one of the agent's answers",
    description="Free text beside the 👍 / 👎, read back when answers are reviewed; null or blank clears it.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such turn for this caller (AI_TURN_NOT_FOUND)"}},
)
async def turn_comment(
    turn_id: int = Path(..., gt=0),
    req: AiTurnCommentReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiTurnCommentResp:
    stored = await AgentService.comment(turn_id, user.user_id, req.comment)
    return AiTurnCommentResp(status=ApiStatus.SUCCESS, message="Note saved",
                             data=AiTurnCommentData(turn_id=turn_id, comment=stored))
