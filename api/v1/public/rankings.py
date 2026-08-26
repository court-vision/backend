from typing import Annotated, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BeforeValidator

from core.rate_limit import PUBLIC_RATE_LIMIT, limiter
from core.responses import respond
from schemas.rankings import RankingsResp
from services.rankings_service import RankingsService
from services.scoring.category_rank import RANKABLE_KEYS

router = APIRouter(prefix="/rankings", tags=["Rankings"])


def _coerce_int(value):
    """Query values arrive as strings; Literal[7, 14, 30] only matches ints."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


RollingWindow = Annotated[Literal[7, 14, 30], BeforeValidator(_coerce_int)]


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
        "over the last N calendar days. Omit for full-season rankings."
    ),
    responses={
        200: {"description": "Rankings retrieved successfully (an empty list with a message before opening night)"},
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
            "Defaults: season 20, L30 8, L14 4, L7 2."
        ),
    ),
) -> RankingsResp:
    keys = parse_categories(categories)
    return respond(await RankingsService.get_rankings(
        window=window, format=format, categories=keys, min_games=min_games,
    ))
