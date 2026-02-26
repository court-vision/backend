from typing import Optional

from fastapi import APIRouter, Query, Request
from schemas.rankings import RankingsResp
from services.rankings_service import RankingsService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/rankings", tags=["Rankings"])


@router.get(
    "/",
    response_model=RankingsResp,
    summary="Get player rankings",
    description=(
        "Returns all NBA players ranked by fantasy points. "
        "Use `window=7`, `window=14`, or `window=30` for rolling averages "
        "over the last N calendar days. Omit for full-season rankings."
    ),
    responses={
        200: {"description": "Rankings retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_rankings(
    request: Request,
    window: Optional[int] = Query(
        None,
        description="Rolling day window for averages: 7, 14, or 30. Omit for full-season.",
        ge=7,
        le=30,
    ),
) -> RankingsResp:
    return await RankingsService.get_rankings(window=window)
