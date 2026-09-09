"""
Streaming picks for a saved team. Clerk-authenticated; ownership via
`get_owned_team`; the league and its credentials are hydrated here, at the
provider boundary, never sent in the body (the legacy `POST /streamers/find`
still takes a `league_info`).
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.streamer import StreamerFindReq, StreamerResp
from services.streamer_service import StreamerService

router = APIRouter(prefix="/teams", tags=["Streamers"])


@router.post("/{team_id}/streamers/find", response_model=StreamerResp)
async def find_team_streamers(req: StreamerFindReq, team: OwnedTeamContext = Depends(get_owned_team)) -> StreamerResp:
    """
    Rank the free agents this team could pick up for the week it is (or will be)
    playing — the same search as `POST /streamers/find`, scoped to a saved team.

    Each candidate says whether the pickup is immediate (`acquisition_status:
    free_agent`) or a waiver claim (`waivers`, clearing on `waivers_until`).
    Before opening night the picks are for week 1 (`upcoming: true`). A 200
    with `data: null` means there is no matchup on the calendar.
    """
    league_info = await load_owned_league_info(team)
    return respond(await StreamerService.find_streamers(
        league_info=league_info,
        fa_count=req.fa_count,
        exclude_injured=req.exclude_injured,
        b2b_only=req.b2b_only,
        mode=req.mode,
        target_day=req.target_day,
        avg_days=req.avg_days,
        team_id=team.team_id,
    ))
