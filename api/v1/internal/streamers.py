from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.clerk_auth import get_current_user
from schemas.breakout import BreakoutResp
from schemas.streamer import StreamerReq, StreamerResp
from services.breakout_service import BreakoutService
from services.streamer_service import StreamerService


router = APIRouter(prefix="/streamers", tags=["Streamers"])


@router.get("/breakout", response_model=BreakoutResp)
async def get_breakout_streamers(
    limit: int = Query(default=20, ge=1, le=50, description="Maximum candidates to return"),
    team: Optional[str] = Query(default=None, description="Filter by NBA team abbreviation (e.g. LAL, BOS)"),
    _: dict = Depends(get_current_user),
) -> BreakoutResp:
    """Return breakout streamer candidates for logged-in Court Vision users."""
    return await BreakoutService.get_breakout_candidates(
        limit=limit,
        team_filter=team.upper() if team else None,
    )


@router.post("/find", response_model=StreamerResp)
async def find_streamers(req: StreamerReq) -> StreamerResp:
    """
    Find and rank the best streaming candidates from free agents.

    Supports two modes:
    - week (default): Rank by rest-of-week value, prioritizing schedule density
      and players on teams with remaining back-to-back games.
    - daily: Rank by single-day pickup value, prioritizing per-game performance.
      Only returns players with a game on the target day. In this mode, B2B
      only means the target day + the next day.

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
