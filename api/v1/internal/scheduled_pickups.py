"""
Scheduled pickups for a saved team: schedule a free-agent add (optionally with a
drop) for a later ESPN day, list what is scheduled and what became of it, cancel
one that has not run. Clerk-authenticated; ownership via `get_owned_team`;
credentials hydrated only here, at the provider boundary. The executor itself is
the pipeline-token route in `jobs.py`.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.scheduled_pickup import SchedulePickupReq, ScheduledPickupListResp, ScheduledPickupResp
from services.scheduled_pickup_service import ScheduledPickupService

router = APIRouter(prefix="/teams", tags=["scheduled pickups"])


@router.post("/{team_id}/pickups", response_model=ScheduledPickupResp)
async def schedule_pickup(req: SchedulePickupReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Schedule a pickup for a later ESPN day. The add/drop is made automatically at
    the earliest moment it counts for that day — the previous day's first tip-off,
    or the rollover into the day when the player to drop plays the day before — and
    is a no-op if the player is gone by then.

    The request is checked against today's board and ESPN's pool before it is stored:
    403 ROSTER_WRITE_DISABLED, 409 ROSTER_WRITE_BLOCKED, 400 SCORING_PERIOD_OUT_OF_RANGE
    (a day past the season), 422 SCHEDULED_PICKUP_INVALID with `data.reason`
    (not_future, add_not_available, drop_not_on_roster, ...), 409
    SCHEDULED_PICKUP_DUPLICATE when that player is already scheduled.
    """
    league_info = await load_owned_league_info(team)
    return respond(await ScheduledPickupService.schedule(team, league_info, req))


@router.get("/{team_id}/pickups", response_model=ScheduledPickupListResp)
async def list_pickups(team: OwnedTeamContext = Depends(get_owned_team)):
    """Pending pickups (soonest day first) and those settled in the last week (newest first)."""
    return respond(await ScheduledPickupService.list_for_team(team))


@router.delete("/{team_id}/pickups/{pickup_id}", response_model=ScheduledPickupResp)
async def cancel_pickup(pickup_id: int, team: OwnedTeamContext = Depends(get_owned_team)):
    """Cancel a pending pickup. 404 when it is not this team's; 409 SCHEDULED_PICKUP_NOT_PENDING
    when it already ran (or was cancelled); 409 SCHEDULED_PICKUP_IN_PROGRESS while an attempt is
    making it — its outcome follows in a few minutes, and a deferred pickup can be cancelled again."""
    return respond(await ScheduledPickupService.cancel(team, pickup_id))
