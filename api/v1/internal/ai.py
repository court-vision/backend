"""The AI layer's routes (docs/AI_LAYER_PLAN.md). Off unless AI_ENABLED is set."""

from fastapi import APIRouter, Depends

from api.deps import UserContext, get_db_user
from schemas.ai import AskReq, AskResp
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
        504: {"description": "The assistant took too long"},
    },
)
async def ask(req: AskReq, user: UserContext = Depends(get_db_user)) -> AskResp:
    return await AiService.ask(req.question, user_id=user.user_id)
