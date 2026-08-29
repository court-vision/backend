"""
GET /v1/rankings/ — the public read path, and the endpoint most exposed to
aggregate load.

Responses are cached as rendered bytes (core/cache.py). The measurements in
docs/PRODUCTION_READINESS.md item 1 put the SQL at 40% of server-side work and
the pydantic build plus serialisation at 60%, so caching the query result would
have left most of the cost in place; caching the body removes all of it. The
data behind it changes once a day, after the post-game pipelines run.
"""

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.cache import ResponseCache
from core.http_cache import render_envelope, serve_cached
from core.rate_limit import PUBLIC_RATE_LIMIT, limiter
from core.responses import respond
from core.settings import settings
from schemas.rankings import RankingsResp, RollingWindow
from services.rankings_service import RankingsService
from services.scoring.category_rank import RANKABLE_KEYS

router = APIRouter(prefix="/rankings", tags=["Rankings"])

RESPONSE_CACHE = ResponseCache(
    "rankings",
    ttl_seconds=settings.rankings_cache_ttl_seconds,
    max_entries=settings.rankings_cache_max_entries,
)


def parse_categories(csv: Optional[str]) -> Optional[list[str]]:
    """Split a comma-separated list of category keys; unknown keys are a 422."""
    if csv is None:
        return None
    keys: list[str] = []
    for raw in csv.split(","):
        key = raw.strip().lower()
        if key and key not in keys:
            keys.append(key)
    unknown = [k for k in keys if k not in RANKABLE_KEYS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category key(s): {', '.join(unknown)}. Allowed: {', '.join(RANKABLE_KEYS)}",
        )
    return keys or None


@router.get(
    "/",
    response_model=RankingsResp,
    summary="Get player rankings",
    description=(
        "Returns all NBA players ranked by fantasy points (`format=points`, the default) "
        "or by summed per-category z-scores for head-to-head category leagues "
        "(`format=categories`). "
        "Use `window=7`, `window=14`, or `window=30` for rolling averages "
        "over the last N calendar days. Omit for full-season rankings.\n\n"
        "Responses are cached briefly and carry an `ETag`; a conditional request "
        "with `If-None-Match` is answered 304."
    ),
    responses={
        200: {"description": "Rankings retrieved successfully (an empty list with a message before opening night)"},
        304: {"description": "Not modified — the client's `If-None-Match` matches the current response"},
        400: {"description": "Rejected by the rankings service"},
        422: {"description": "Invalid window, format, or category key"},
        429: {"description": "Rate limit exceeded"},
    },
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_rankings(
    request: Request,
    window: Optional[RollingWindow] = Query(
        None,
        description="Rolling day window for averages: 7, 14, or 30. Omit for full-season.",
    ),
    format: Literal["points", "categories"] = Query(
        "points",
        description="Ranking format: `points` (fantasy points) or `categories` (sum of category z-scores).",
    ),
    categories: Optional[str] = Query(
        None,
        description=(
            "Comma-separated category keys for `format=categories`; defaults to standard 9-cat. "
            f"Allowed: {', '.join(RANKABLE_KEYS)}."
        ),
    ),
    min_games: Optional[int] = Query(
        None,
        ge=1,
        le=82,
        description=(
            "Minimum games played to be ranked (`format=categories` only). "
            "Defaults to 1 for every window — the floor is an opt-in filter."
        ),
    ),
):
    keys = parse_categories(categories)

    if not RESPONSE_CACHE.enabled:
        return respond(await RankingsService.get_rankings(
            window=window, format=format, categories=keys, min_games=min_games,
        ))

    # `categories` and `min_games` only shape a category response; folding them
    # away for points keeps 82 min_games values from becoming 82 identical entries.
    cache_key = (
        (window, format, tuple(keys) if keys else None, min_games)
        if format == "categories" else (window, format, None, None)
    )

    async def render() -> bytes:
        return await render_envelope("rankings.serialize", await RankingsService.get_rankings(
            window=window, format=format, categories=keys, min_games=min_games,
        ))

    return await serve_cached(request, RESPONSE_CACHE, cache_key, render)
