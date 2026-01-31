from fastapi import APIRouter, Request
from schemas.rankings import RankingsResp
from services.rankings_service import RankingsService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/rankings", tags=["Rankings"])


@router.get(
    "/",
    response_model=RankingsResp,
    summary="Get player rankings",
    description="Returns all NBA players ranked by fantasy points. Updated daily after games complete.",
    responses={
        200: {"description": "Rankings retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_rankings(request: Request) -> RankingsResp:
    return await RankingsService.get_rankings()
