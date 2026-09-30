"""The AI layer's routes (docs/AI_LAYER_PLAN.md). Off unless AI_ENABLED is set."""

from fastapi import APIRouter, Body, Depends, Path

from api.deps import UserContext, get_db_user
from core.errors import NotFoundError
from schemas.ai import AiFeedbackData, AiFeedbackReq, AiFeedbackResp, AskReq, AskResp, RouteReq, RouteResp
from schemas.common import ApiStatus
from services.ai import questions
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
