from fastapi import APIRouter, Query
from typing import Optional
from services.playoff_service import PlayoffService
from schemas.playoff import PlayoffBracketResp

router = APIRouter(prefix="/playoff", tags=["playoff"])


@router.get("/bracket", response_model=PlayoffBracketResp)
async def get_playoff_bracket(
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
    return await PlayoffService.get_bracket(season)
