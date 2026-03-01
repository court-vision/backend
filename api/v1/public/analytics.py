"""
Public API routes for analytics (API key required).
"""

from fastapi import APIRouter, Request, Security

from schemas.optimize import OptimizeResp, GenerateLineupRequest
from services.optimize_service import OptimizeService
from core.rate_limit import limiter, API_KEY_RATE_LIMIT
from core.api_key_auth import require_scope

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.post(
    "/generate-lineup",
    response_model=OptimizeResp,
    summary="Generate optimized lineup",
    description="Auto-fetch roster and free agents from stored ESPN/Yahoo credentials, then generate an optimized lineup. Requires API key with 'optimize' scope.",
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
    api_key=Security(require_scope("optimize")),
) -> OptimizeResp:
    """Generate optimized lineup using stored team credentials."""
    return await OptimizeService.optimize_from_team(api_key, body)
