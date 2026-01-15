from fastapi import APIRouter

from services.streamer_service import StreamerService
from schemas.streamer import StreamerReq, StreamerResp


router = APIRouter(prefix="/streamers", tags=["Streamers"])


@router.post("/find", response_model=StreamerResp)
async def find_streamers(req: StreamerReq) -> StreamerResp:
    """
    Find and rank the best streaming candidates from free agents.

    Returns a ranked list of free agents sorted by streaming value,
    prioritizing players on teams with remaining back-to-back games
    and strong recent performance.

    Request body:
    - league_info: ESPN league credentials
    - fa_count: Number of free agents to analyze (default 50)
    - exclude_injured: Exclude injured players (default true)
    - b2b_only: Only show B2B team players (default false)
    - day: Day index within matchup (0-indexed). If null, uses current day.
    - avg_days: Number of days for rolling average (default 7, range 3-30)
    """
    return await StreamerService.find_streamers(
        league_info=req.league_info,
        fa_count=req.fa_count,
        exclude_injured=req.exclude_injured,
        b2b_only=req.b2b_only,
        day=req.day,
        avg_days=req.avg_days
    )
