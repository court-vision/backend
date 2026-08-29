"""
GET /v1/internal/rankings/{team_id} — the public rankings pool, scored by the
caller's league instead of the platform default.

The public endpoint ranks on stored `fpts`, which is one hardcoded points
formula. A league that scores differently — different weights, or categories
entirely — needs the same players ranked by its own settings. The scoring
engine already does that for matchups, streamers and lineups; this puts it on
the rankings page.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from api.deps import OwnedTeamContext, get_owned_team
from core.cache import ResponseCache
from core.http_cache import render_envelope, serve_cached
from core.responses import respond
from core.settings import settings
from schemas.rankings import RankingsResp, RollingWindow
from services.rankings_service import RankingsService
from services.scoring.resolver import resolve_scoring

router = APIRouter(prefix="/rankings", tags=["Rankings"])

# Keyed by league scoring, not by user (see the cache key below), so this bounds
# distinct league configurations rather than sign-ups.
RESPONSE_CACHE = ResponseCache(
    "rankings.league",
    ttl_seconds=settings.rankings_cache_ttl_seconds,
    max_entries=settings.league_rankings_cache_max_entries,
)


@router.get(
    "/{team_id}",
    response_model=RankingsResp,
    summary="Get player rankings under a team's league scoring",
    description=(
        "Ranks every NBA player by the scoring settings of the league this team belongs to: "
        "the league's point weights, or its categories for a head-to-head category league. "
        "`meta.scoring` reports which was used, whether the league's settings were "
        "successfully read from the provider, and any scoring keys that could not be honored.\n\n"
        "The format is the league's — use the public `/v1/rankings/` endpoint to rank by an "
        "arbitrary format or category set."
    ),
    responses={
        200: {"description": "Rankings retrieved successfully"},
        304: {"description": "Not modified — the client's `If-None-Match` matches the current response"},
        400: {"description": "Rejected by the rankings service"},
        404: {"description": "No such team, or it does not belong to the caller"},
        422: {"description": "Invalid window or min_games"},
    },
)
async def get_league_rankings(
    request: Request,
    window: Optional[RollingWindow] = Query(
        None,
        description="Rolling day window for averages: 7, 14, or 30. Omit for full-season.",
    ),
    min_games: Optional[int] = Query(
        None,
        ge=1,
        le=82,
        description="Minimum games played to be ranked. Defaults to 1 — the floor is an opt-in filter.",
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
):
    # `get_owned_team` already loaded the league, so resolving its scoring is
    # pure: no second trip to the database for what the dependency just read.
    scoring = resolve_scoring(team.league)

    if not RESPONSE_CACHE.enabled:
        return respond(await RankingsService.get_league_rankings(
            scoring, window=window, min_games=min_games,
        ))

    # Two teams in one league — and two leagues that score alike — share an
    # entry, because nothing user-specific is in the body. The fingerprint means
    # a re-sync that changes the settings lands on a new key rather than serving
    # numbers computed from the old ones.
    ignores_min_games = not scoring.is_categories and scoring.points.is_default
    cache_key = (
        team.league_id,
        window,
        None if ignores_min_games else min_games,
        scoring.fingerprint,
    )

    async def render() -> bytes:
        return await render_envelope(
            "rankings.league.serialize",
            await RankingsService.get_league_rankings(scoring, window=window, min_games=min_games),
        )

    return await serve_cached(request, RESPONSE_CACHE, cache_key, render, private=True)
