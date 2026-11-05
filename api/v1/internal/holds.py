"""
Dime's holds on a saved team (the Dime autopilot plan, step 4; services/steering.py):
list the live ones, apply a `steer` chip (its holds set and its clears done, the
chip marked sent), clear one. Clerk-authenticated; ownership via `get_owned_team`.
No ESPN call: holds steer Court Vision's own automations, never ESPN directly.

While steering is switched off (`DIME_STEERING_ENABLED`) the list is empty and a
chip can't be applied (409 AI_STEERING_OFF); a hold can always be cleared.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team
from core.errors import NotFoundError
from core.responses import respond
from schemas.team_hold import ApplyHoldsReq, TeamHoldsResp
from services import steering
from services.ai import steering as ai_steering

router = APIRouter(prefix="/teams", tags=["holds"])


@router.get("/{team_id}/holds", response_model=TeamHoldsResp)
async def list_holds(team: OwnedTeamContext = Depends(get_owned_team)):
    """The team's live holds (today's or later, not cleared), by first day, each with what it
    does and Dime's reason."""
    return respond(await ai_steering.desk_holds(team.team_id))


@router.post("/{team_id}/holds", response_model=TeamHoldsResp)
async def apply_holds(req: ApplyHoldsReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Apply a `steer` chip from the AI desk: its holds are set (each with the chip's reason)
    and the holds it clears are cleared, in one transaction, and the chip is marked sent.
    Returns the team's live holds then. 404 AI_PROPOSAL_NOT_FOUND (not the caller's, or not
    this team's); 409 AI_PROPOSAL_NOT_PENDING (already applied, dismissed, expired, or a
    check-in's shadow); 422 AI_PROPOSAL_MISMATCH (not a steer chip); 409 AI_STEERING_OFF.
    """
    made = await ai_steering.apply_holds(team, req.proposal_id)
    return respond(await ai_steering.desk_holds(team.team_id, f"{made} hold(s) set"))


@router.delete("/{team_id}/holds/{hold_id}", response_model=TeamHoldsResp)
async def clear_hold(hold_id: int, team: OwnedTeamContext = Depends(get_owned_team)):
    """Clear one of the team's holds: from now on nothing reads it. Clearing one already cleared
    or past its last day changes nothing. 404 HOLD_NOT_FOUND when it isn't this team's."""
    row = await steering.clear(team.team_id, hold_id, by="user")
    if row is None:
        raise NotFoundError("HOLD_NOT_FOUND", "Hold not found")
    return respond(await ai_steering.desk_holds(team.team_id, f"Hold on {row.player_name} cleared"))
