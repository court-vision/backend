"""
Public API routes for game information.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Request, Path
from schemas.games import GamesOnDateResp
from services.games_service import GamesService
from core import public_read_cache as caches
from core.http_cache import serve_cached_envelope
from core.nba_calendar import nba_date_et
from core.responses import respond
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
    today = nba_date_et()
    if game_date == today:
        # Today's slate carries the live scoreboard overlay: never cached, and no
        # cache headers, exactly as before.
        return respond(await GamesService.get_games_on_date(game_date))
    # Two or more nights back the games are final. Last night's stay on the
    # short TTL: the post-game run that writes their final scores can still be
    # retrying after the 6 AM ET date flip (cron-runner asks until mid-morning).
    settled = game_date < today - timedelta(days=1)
    return await serve_cached_envelope(
        request, caches.GAMES_SETTLED if settled else caches.GAMES, caches.key(game_date),
        lambda: GamesService.get_games_on_date(game_date),
    )
