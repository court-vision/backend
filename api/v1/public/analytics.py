"""
Public API routes for analytics (API key required).
"""

from typing import Optional

from fastapi import APIRouter, Query, Request, Security

from schemas.breakout import BreakoutResp
from schemas.optimize import OptimizeResp, GenerateLineupRequest
from services.breakout_service import BreakoutService
from services.optimize_service import OptimizeService
from core.rate_limit import limiter, API_KEY_RATE_LIMIT
from core.api_key_auth import require_scope

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.post(
    "/generate-lineup",
    response_model=OptimizeResp,
    summary="Generate optimized lineup",
    description="Auto-fetch roster and free agents from stored ESPN/Yahoo credentials, then generate an optimized lineup. Requires API key with 'analytics' scope.",
    responses={
        200: {"description": "Lineup generated successfully"},
        401: {"description": "API key required or invalid"},
        403: {"description": "API key lacks required scope"},
        404: {"description": "Team not found or does not belong to this API key"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(API_KEY_RATE_LIMIT)
async def generate_lineup(
    request: Request,
    body: GenerateLineupRequest,
    api_key=Security(require_scope("analytics")),
) -> OptimizeResp:
    """Generate optimized lineup using stored team credentials."""
    return await OptimizeService.optimize_from_team(api_key, body)


@router.get(
    "/breakout-streamers",
    response_model=BreakoutResp,
    summary="Breakout streamer candidates",
    description=(
        "Returns players likely to see increased minutes due to a prominent teammate "
        "being injured or suspended. Updated daily. Requires API key with 'analytics' scope."
    ),
    responses={
        200: {"description": "Breakout candidates retrieved successfully"},
        401: {"description": "API key required or invalid"},
        403: {"description": "API key lacks required scope"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(API_KEY_RATE_LIMIT)
async def get_breakout_streamers(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50, description="Maximum candidates to return"),
    team: Optional[str] = Query(default=None, description="Filter by NBA team abbreviation (e.g. LAL, BOS)"),
    _api_key=Security(require_scope("analytics")),
) -> BreakoutResp:
    """Return breakout streamer candidates."""
    return await BreakoutService.get_breakout_candidates(
        limit=limit,
        team_filter=team.upper() if team else None,
    )
