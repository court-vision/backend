"""
Public API routes for game information.
"""

from datetime import date

from fastapi import APIRouter, Request, Path
from schemas.games import GamesOnDateResp
from services.games_service import GamesService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/games", tags=["Games"])


@router.get(
    "/{game_date}",
    response_model=GamesOnDateResp,
    summary="Get games on a date",
    description="Returns all NBA games scheduled for or played on a specific date.",
    responses={
        200: {"description": "Games retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_games_on_date(
    request: Request,
    game_date: date = Path(..., description="Date in YYYY-MM-DD format"),
) -> GamesOnDateResp:
    """Get all games on a specific date."""
    return await GamesService.get_games_on_date(game_date)
