from fastapi import APIRouter

from services.streamer_service import StreamerService
from schemas.streamer import StreamerReq, StreamerResp


router = APIRouter(prefix="/streamers", tags=["Streamers"])


@router.post("/find", response_model=StreamerResp)
async def find_streamers(req: StreamerReq) -> StreamerResp:
    """
    Find and rank the best streaming candidates from free agents.

    Supports two modes:
    - week (default): Rank by rest-of-week value, prioritizing schedule density
      and players on teams with remaining back-to-back games.
    - daily: Rank by single-day pickup value, prioritizing per-game performance.
      Only returns players with a game on the target day.

    Request body:
    - league_info: ESPN/Yahoo league credentials
    - fa_count: Number of free agents to analyze (default 50)
    - exclude_injured: Exclude injured players (default true)
    - b2b_only: Only show B2B team players (default false)
    - mode: Scoring mode - 'week' or 'daily' (default 'week')
    - target_day: Day index for daily mode (0-indexed). If null, uses current day.
    - avg_days: Number of days for rolling average (default 7, range 3-30)
    """
    return await StreamerService.find_streamers(
        league_info=req.league_info,
        fa_count=req.fa_count,
        exclude_injured=req.exclude_injured,
        b2b_only=req.b2b_only,
        mode=req.mode,
        target_day=req.target_day,
        avg_days=req.avg_days
    )
