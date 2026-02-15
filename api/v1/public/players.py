from typing import Optional
from fastapi import APIRouter, Query, Request, Path
from schemas.player import PlayerStatsResp
from schemas.players_list import PlayersListResp
from schemas.player_games import PlayerGamesResp
from schemas.player_trends import PlayerTrendsResp
from services.player_service import PlayerService
from services.players_list_service import PlayersListService
from services.player_games_service import PlayerGamesService
from services.trends_service import TrendsService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT

router = APIRouter(prefix="/players", tags=["Players"])


@router.get(
    "/",
    response_model=PlayersListResp,
    summary="List players",
    description="List NBA players with optional filters for team, position, games played, and name search.",
    responses={
        200: {"description": "Players retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def list_players(
    request: Request,
    team: Optional[str] = Query(None, description="Filter by team abbreviation (e.g., LAL)"),
    position: Optional[str] = Query(None, description="Filter by position (G, F, C)"),
    min_games: Optional[int] = Query(None, ge=1, description="Minimum games played"),
    search: Optional[str] = Query(None, description="Search by player name"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> PlayersListResp:
    """List players with optional filters."""
    return await PlayersListService.list_players(
        team=team,
        position=position,
        min_games=min_games,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/stats",
    response_model=PlayerStatsResp,
    summary="Get player statistics",
    description="Retrieve detailed statistics for a player by ID or name/team combination. "
    "Use the `window` parameter to get averages over a specific game window.",
    responses={
        200: {"description": "Player stats retrieved successfully"},
        404: {"description": "Player not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_stats_by_query(
    request: Request,
    espn_id: Optional[int] = Query(None, description="ESPN player ID (preferred for internal lookups)"),
    player_id: Optional[int] = Query(None, description="Player ID (alias for espn_id, for backwards compatibility)"),
    name: Optional[str] = Query(None, description="Player name (used for public/roster lookup)"),
    team: Optional[str] = Query(None, description="Player team abbreviation (used with name)"),
    window: str = Query("season", description="Stat window for averages: season, l5, l10, l15, l20", pattern="^(season|l5|l10|l15|l20)$"),
) -> PlayerStatsResp:
    """
    Get player stats by ID or by name/team combination.

    Lookup priority:
    1. espn_id - ESPN player ID (most reliable for internal use)
    2. player_id - Alias for espn_id (backwards compatibility)
    3. name + team - Name-based lookup (for public queries)

    The `window` parameter controls which games are used to compute averages:
    - `season` (default): Full season averages
    - `l5`: Last 5 games
    - `l10`: Last 10 games
    - `l15`: Last 15 games
    - `l20`: Last 20 games

    Game logs are always returned in full regardless of window.
    Advanced stats (net rating, usage, PIE, etc.) are always season-level.
    """
    return await PlayerService.get_player_stats(espn_id=espn_id, player_id=player_id, name=name, team=team, window=window)


@router.get(
    "/{player_id}/stats",
    response_model=PlayerStatsResp,
    summary="Get player statistics by ID",
    description="Retrieve detailed statistics for a player by their ID.",
    responses={
        200: {"description": "Player stats retrieved successfully"},
        404: {"description": "Player not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_stats(request: Request, player_id: int) -> PlayerStatsResp:
    """Get player stats by ID (legacy endpoint for backwards compatibility)."""
    return await PlayerService.get_player_stats(player_id=player_id)


@router.get(
    "/{player_id}/games",
    response_model=PlayerGamesResp,
    summary="Get player game log",
    description="Retrieve the recent game log for a player showing box scores for each game.",
    responses={
        200: {"description": "Game log retrieved successfully"},
        404: {"description": "Player not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_games(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
    limit: int = Query(10, ge=1, le=50, description="Number of games to return"),
) -> PlayerGamesResp:
    """Get game log for a player."""
    return await PlayerGamesService.get_player_games(player_id=player_id, limit=limit)


@router.get(
    "/{player_id}/trends",
    response_model=PlayerTrendsResp,
    summary="Get player trends",
    description="Retrieve trend data for a player including performance over different periods and ownership changes.",
    responses={
        200: {"description": "Trends retrieved successfully"},
        404: {"description": "Player not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_trends(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
) -> PlayerTrendsResp:
    """Get trend data for a player."""
    return await TrendsService.get_player_trends(player_id=player_id)
