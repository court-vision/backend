"""
Public API routes for ownership trends.
"""

from typing import Literal

from fastapi import APIRouter, Request, Query
from schemas.ownership import OwnershipTrendingResp
from services.ownership_service import OwnershipService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/ownership", tags=["Ownership"])


@router.get(
    "/trending",
    response_model=OwnershipTrendingResp,
    summary="Get trending players by ownership",
    description="""
Returns players with significant ownership changes over a specified period.

Uses velocity-based ranking by default, which measures relative change:
- 5% → 15% = +200% velocity (breakout player)
- 60% → 65% = +8% velocity (established player with small uptick)

This surfaces emerging players better than absolute change alone.
    """,
    responses={
        200: {"description": "Trending players retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_ownership_trending(
    request: Request,
    days: int = Query(7, ge=1, le=30, description="Lookback period in days"),
    min_change: float = Query(5.0, ge=0, le=100, description="Minimum ownership change in percentage points"),
    min_ownership: float = Query(3.0, ge=0, le=100, description="Minimum ownership % to filter noise from deep roster players"),
    sort_by: Literal["velocity", "change"] = Query("velocity", description="Sort by velocity (relative change) or change (absolute)"),
    direction: Literal["up", "down", "both"] = Query("both", description="Trend direction"),
    limit: int = Query(20, ge=1, le=50, description="Maximum players per direction"),
) -> OwnershipTrendingResp:
    """Get players with trending ownership using velocity-based ranking."""
    return await OwnershipService.get_trending(
        days=days,
        min_change=min_change,
        min_ownership=min_ownership,
        sort_by=sort_by,
        direction=direction,
        limit=limit,
    )
