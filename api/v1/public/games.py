"""
Public API routes for game information.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Request, Path
from schemas.common import ApiStatus
from schemas.games import GameDetailResp, GamesOnDateResp
from services.game_detail_service import GameDetailService
from services.games_service import GamesService
from core import public_read_cache as caches
from core.http_cache import Uncacheable, serve_cached, serialize_model, serve_cached_envelope
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


@router.get(
    "/{game_date}/{game_id}",
    response_model=GameDetailResp,
    summary="Get one game",
    description=(
        "One game of a night: its score, the score by period, the score through the game, and every "
        "player's line. A finished game comes back as it stood at the final buzzer: the lines are the "
        "settled game log once the post-game run has written it, the live box score's last read before."
    ),
    responses={
        200: {"description": "Game retrieved successfully"},
        404: {"description": "No such game on this date"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_game(
    request: Request,
    game_date: date = Path(..., description="Date in YYYY-MM-DD format"),
    game_id: str = Path(..., pattern=r"^\d{10}$", description="NBA game ID (e.g. 0022600001)"),
) -> GameDetailResp:
    """Get one game, frozen at the final buzzer once it is over."""
    today = nba_date_et()
    produce = lambda: GameDetailService.get_game(game_date, game_id)  # noqa: E731
    cache = caches.GAME_DETAIL_SETTLED if game_date < today - timedelta(days=1) else caches.GAME_DETAIL
    if not cache.enabled:
        return respond(await produce())

    async def render() -> bytes:
        resp = await produce()
        # Kept only once the game is over and whole: tonight's games move, and
        # a final whose period line the league feed did not give is asked again.
        game = resp.data.game if resp.status == ApiStatus.SUCCESS and resp.data else None
        if game is None or game.status != "final" or not resp.data.home_periods:
            raise Uncacheable(resp)
        return serialize_model(resp)

    return await serve_cached(request, cache, caches.key(game_date, game_id), render)
