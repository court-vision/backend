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
    description="Returns players with significant ownership changes over a specified period.",
    responses={
        200: {"description": "Trending players retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_ownership_trending(
    request: Request,
    days: int = Query(7, ge=1, le=30, description="Lookback period in days"),
    min_change: float = Query(5.0, ge=0, le=100, description="Minimum ownership change percentage"),
    direction: Literal["up", "down", "both"] = Query("both", description="Trend direction"),
    limit: int = Query(20, ge=1, le=50, description="Maximum players per direction"),
) -> OwnershipTrendingResp:
    """Get players with trending ownership."""
    return await OwnershipService.get_trending(
        days=days,
        min_change=min_change,
        direction=direction,
        limit=limit,
    )
