from typing import Optional

from fastapi import APIRouter, Query, Request

from schemas.breakout import BreakoutResp
from services.breakout_service import BreakoutService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/breakout-streamers", tags=["Streamers"])


@router.get(
    "/",
    response_model=BreakoutResp,
    summary="Get breakout streamer candidates",
    description=(
        "Returns players likely to see increased minutes due to a prominent teammate "
        "being injured or suspended. Candidates are identified by analyzing the "
        "minutes vacuum created when a starter (28+ min/game) goes OUT, then "
        "ranking their teammates by positional fit and historical performance "
        "during previous absences. Updated daily by the breakout-detection pipeline."
    ),
    responses={
        200: {"description": "Breakout candidates retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_breakout_streamers(
    request: Request,
    limit: int = Query(20, ge=1, le=50, description="Maximum candidates to return"),
    team: Optional[str] = Query(
        None,
        description="Filter by NBA team abbreviation (e.g. LAL, BOS)",
    ),
) -> BreakoutResp:
    """Return breakout streamer candidates based on current injury data."""
    return await BreakoutService.get_breakout_candidates(
        limit=limit,
        team_filter=team.upper() if team else None,
    )
