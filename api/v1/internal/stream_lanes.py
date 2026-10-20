"""
Stream lanes for a saved team: a standing order to keep one roster spot streaming
for the rest of the matchup, up to four spots per team, planned together. Preview
what a lane would do, start one, list them with their history, pause / resume /
change how one tells you, stop it. Clerk-authenticated;
ownership via `get_owned_team`; credentials hydrated only here, at the provider
boundary. The decisions themselves run in the scheduled-pickups executor
(`jobs.py`), before each day of the matchup.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.stream_lane import (
    StreamLaneListResp,
    StreamLanePlanReq,
    StreamLanePlanResp,
    StreamLaneResp,
    StreamLaneStartReq,
    StreamLaneUpdateReq,
)
from services.stream_lane_service import StreamLaneService

router = APIRouter(prefix="/teams", tags=["stream lanes"])


@router.post("/{team_id}/stream-lanes/plan", response_model=StreamLanePlanResp)
async def plan_stream_lane(req: StreamLanePlanReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    What a lane started now on `player_id`'s spot would do: who would hold the spot each
    remaining day of the matchup, what each adds to that day's lineup, the gain over
    keeping the spot's player, and the adds it would use. It is planned with the team's
    open lanes that decide its first day too (they share the adds); `others` is their
    projections then. A projection only — the lanes re-plan before each day with the
    players available then. Nothing is stored.

    422 STREAM_LANE_INVALID with `data.reason` (not_on_roster, on_ir, no_matchup_end,
    matchup_over); 409 STREAM_LANE_EXISTS (a lane already streams that spot),
    STREAM_LANE_LIMIT (the team has four open lanes), ROSTER_WRITE_BLOCKED for a league
    whose rosters can't be changed from here.
    """
    league_info = await load_owned_league_info(team)
    return respond(await StreamLaneService.plan(team, league_info, req))


@router.post("/{team_id}/stream-lanes", response_model=StreamLaneResp)
async def start_stream_lane(req: StreamLaneStartReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Start streaming `player_id`'s spot through the matchup's last day. The first decision
    is made when a pickup for the first open day can first be made (possibly at once).
    `notify: desk` keeps the lane from emailing.

    Besides the plan route's errors: 403 ROSTER_WRITE_DISABLED, 422 STREAM_LANE_INVALID
    `scheduled_out` (a pickup already scheduled drops that player).
    """
    league_info = await load_owned_league_info(team)
    return respond(await StreamLaneService.start(team, league_info, req))


@router.get("/{team_id}/stream-lanes", response_model=StreamLaneListResp)
async def list_stream_lanes(team: OwnedTeamContext = Depends(get_owned_team)):
    """The open lanes (active or paused, oldest first) and lanes that ended in the last week,
    each with its latest projection and its events (newest first)."""
    return respond(await StreamLaneService.list_for_team(team))


@router.patch("/{team_id}/stream-lanes/{lane_id}", response_model=StreamLaneResp)
async def update_stream_lane(lane_id: int, req: StreamLaneUpdateReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Pause (`status: paused`) or resume (`status: active`) a lane, or change `notify`.
    Pausing cancels a pickup the lane scheduled that hasn't run, and that day is decided
    again on resume; 409 STREAM_LANE_BUSY while the pickup is being made. Resuming needs
    the spot's player still on the roster (422 STREAM_LANE_INVALID `holder_gone`) and no
    pickup scheduled that drops them (`scheduled_out`); a lane resumed after its matchup's
    last open day ends instead. 404 STREAM_LANE_NOT_FOUND; 409 STREAM_LANE_ENDED.
    """
    league_info = await load_owned_league_info(team)
    return respond(await StreamLaneService.update(team, league_info, lane_id, req))


@router.delete("/{team_id}/stream-lanes/{lane_id}", response_model=StreamLaneResp)
async def stop_stream_lane(lane_id: int, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Stop a lane. A pickup it scheduled that hasn't run is cancelled with it; the player
    holding the spot stays on the roster. 409 STREAM_LANE_BUSY while that pickup is being
    made (try again in a few minutes); 404 STREAM_LANE_NOT_FOUND; 409 STREAM_LANE_ENDED.
    """
    return respond(await StreamLaneService.stop(team, lane_id))
