"""
Public API routes for analytics (API key required).
"""

from fastapi import APIRouter, Request, Security

from schemas.optimize import OptimizeResp, OptimizeRequest
from services.optimize_service import OptimizeService
from core.rate_limit import limiter, API_KEY_RATE_LIMIT
from core.api_key_auth import require_scope

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.post(
    "/optimize",
    response_model=OptimizeResp,
    summary="Optimize lineup",
    description="Generate an optimized lineup for a fantasy week. Requires API key with 'optimize' scope.",
    responses={
        200: {"description": "Lineup optimized successfully"},
        401: {"description": "API key required or invalid"},
        403: {"description": "API key lacks required scope"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(API_KEY_RATE_LIMIT)
async def optimize_lineup(
    request: Request,
    body: OptimizeRequest,
    api_key=Security(require_scope("optimize")),
) -> OptimizeResp:
    """Optimize lineup for a fantasy week."""
    return await OptimizeService.optimize_lineup(body)
