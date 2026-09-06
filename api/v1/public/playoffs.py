from core.responses import respond
from fastapi import APIRouter, Query, Request
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT
from typing import Optional
from services.playoff_service import PlayoffService
from schemas.playoff import PlayoffBracketResp

router = APIRouter(prefix="/playoff", tags=["playoff"])


@router.get(
    "/bracket", response_model=PlayoffBracketResp,
    responses={404: {"description": "No bracket data for the requested season"}, 429: {"description": "Rate limit exceeded"}},
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_playoff_bracket(
    request: Request,
    season: Optional[str] = Query(
        default=None,
        pattern=r"^\d{4}-\d{2}$",
        description="Season string (e.g. '2025-26'). Omit for latest.",
    )
) -> PlayoffBracketResp:
    """
    Get the NBA playoff bracket with current series standings.

    Returns all rounds (First Round through Finals), grouped by conference.
    Updated once nightly after games complete via PlayoffBracketPipeline.
    """
    return respond(await PlayoffService.get_bracket(season))
