from typing import Optional
from datetime import date
from fastapi import APIRouter, Query, Request, Path
from schemas.player import PlayerStatsResp, PlayerPercentilesResp, PlayerStatusResp, PlayerOwnershipResp
from schemas.players_list import PlayersListResp
from schemas.player_games import PlayerGamesResp
from schemas.player_trends import PlayerTrendsResp
from schemas.market import PlayerProjectionResp, SeasonKey
from services.public_market_service import PublicMarketService
from services.player_service import PlayerService
from services.players_list_service import PlayersListService
from services.player_games_service import PlayerGamesService
from services.trends_service import TrendsService
from services.ownership_service import OwnershipService
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT
from core.responses import respond

router = APIRouter(prefix="/players", tags=["Players"])


@router.get(
    "/{player_id}/projection",
    response_model=PlayerProjectionResp,
    summary="Get a player's ESPN preseason projection",
    description=(
        "Projected games and per-game statistics from the latest ESPN projection snapshot "
        "in the requested season. Shooting rates are recomputed from makes/attempts on a 0–1 scale. "
        "Missing measurements remain null. A known player without a projection returns success "
        "with data=null; an unknown NBA player ID returns 404."
    ),
    responses={404: {"description": "Player not found"}, 429: {"description": "Rate limit exceeded"}},
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_projection(
    request: Request,
    player_id: int = Path(..., gt=0, description="NBA player ID"),
    season: Optional[SeasonKey] = Query(None, description="NBA season; defaults to the configured active season"),
    as_of: Optional[date] = Query(None, description="Latest snapshot on or before this date; omit for latest"),
) -> PlayerProjectionResp:
    return respond(await PublicMarketService.get_projection(player_id=player_id, season=season, as_of=as_of))


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
    name: Optional[str] = Query(None, description="Search by player name"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> PlayersListResp:
    """List players with optional filters."""
    return respond(await PlayersListService.list_players(
        team=team,
        position=position,
        min_games=min_games,
        name=name,
        limit=limit,
        offset=offset,
    ))


@router.get(
    "/stats",
    response_model=PlayerStatsResp,
    summary="Get player statistics",
    description="Retrieve detailed statistics for a player by ID or name/team combination. "
    "Use the `window` parameter to get averages over a specific game window.",
    responses={
        200: {"description": "Player stats retrieved successfully"},
        400: {"description": "No player identifier given"},
        404: {"description": "Player not found, or no game stats for the player"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_stats_by_query(
    request: Request,
    espn_id: Optional[int] = Query(None, description="ESPN player ID; takes precedence over player_id"),
    player_id: Optional[int] = Query(None, description="NBA player ID (distinct from espn_id)"),
    name: Optional[str] = Query(None, description="Player name (used for public/roster lookup)"),
    team: Optional[str] = Query(None, description="Player team abbreviation (used with name)"),
    window: str = Query("season", description="Stat window for averages: 'season' for full season, or 'lN' for last N games (e.g. l5, l10, l15)", pattern="^(season|l[1-9][0-9]?)$"),
) -> PlayerStatsResp:
    """
    Get player stats by ID or by name/team combination.

    Lookup priority:
    1. espn_id - ESPN player ID
    2. player_id - NBA player ID
    3. name + team - Name-based lookup (for public queries)

    The `window` parameter controls which games are used to compute averages:
    - `season` (default): Full season averages
    - `lN`: Last N games (e.g. l5, l10, l15, l20, l30)

    Game logs are always returned in full regardless of window.
    Advanced stats (net rating, usage, PIE, etc.) are always season-level.
    """
    return respond(await PlayerService.get_player_stats(espn_id=espn_id, player_id=player_id, name=name, team=team, window=window))


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
async def get_player_stats(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
    window: str = Query("season", pattern="^(season|l[1-9][0-9]?)$", description="Full season or last N games (l5, l10, etc.)"),
) -> PlayerStatsResp:
    """Get player stats by NBA ID with the same windows as the query endpoint."""
    return respond(await PlayerService.get_player_stats(player_id=player_id, window=window))


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
    return respond(await PlayerGamesService.get_player_games(player_id=player_id, limit=limit))


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
    return respond(await TrendsService.get_player_trends(player_id=player_id))


@router.get(
    "/{player_id}/percentiles",
    response_model=PlayerPercentilesResp,
    summary="Get player percentile ranks",
    description="Get percentile ranks for a player's stats compared to all qualifying players.",
    responses={
        200: {"description": "Percentiles calculated successfully"},
        404: {"description": "Player not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_percentiles(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
    min_games: int = Query(20, ge=1, description="Minimum games played to qualify"),
) -> PlayerPercentilesResp:
    """Get percentile ranks for a player's stats vs the league."""
    return respond(await PlayerService.get_player_percentiles(player_id=player_id, min_games=min_games))


@router.get(
    "/{player_id}/status",
    response_model=PlayerStatusResp,
    summary="Get player injury status",
    description="Get the most recent injury status for a player.",
    responses={
        200: {"description": "Player status retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_status(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
) -> PlayerStatusResp:
    """Get the most recent injury status for a player."""
    return respond(await PlayerService.get_player_status(player_id=player_id))


@router.get(
    "/{player_id}/ownership",
    response_model=PlayerOwnershipResp,
    summary="Get player ownership",
    description="Get the current fantasy ownership percentage for a player.",
    responses={
        200: {"description": "Ownership data retrieved successfully"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_player_ownership(
    request: Request,
    player_id: int = Path(..., description="NBA player ID"),
    days: int = Query(14, ge=1, le=30, description="Lookback window in days for trend comparison"),
) -> PlayerOwnershipResp:
    """Get current ownership percentage for a player."""
    return respond(await OwnershipService.get_player_ownership(player_id=player_id, days=days))
