"""
Protected players on a saved team: the roster lock (services/roster_protection.py, migration
0048). Clerk-authenticated; ownership via `get_owned_team`.

A protected player is never dropped or suggested as a drop by Court Vision (the Week desk,
scheduled pickups, stream lanes, Dime) until the user unprotects him. Protecting him cancels
what would drop him: the pending pickups that drop him and the stream lanes holding his spot.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.roster_protection import RosterProtectionsResp
from services.roster_protection import RosterProtectionService

router = APIRouter(prefix="/teams", tags=["protections"])


@router.get("/{team_id}/protections", response_model=RosterProtectionsResp)
async def list_protections(team: OwnedTeamContext = Depends(get_owned_team)):
    """The team's protected players, by name. No ESPN call."""
    return respond(await RosterProtectionService.list_for_team(team))


@router.put("/{team_id}/protections/{player_id}", response_model=RosterProtectionsResp)
async def protect_player(player_id: int, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Protect `player_id` (ESPN) on this team. He must be on the roster: today's on ESPN, or
    tomorrow's once today's first game has tipped (a player added tonight). Protecting one
    already protected changes nothing.

    What would drop him goes: the open stream lanes holding his spot are stopped and the
    pending pickups dropping him are cancelled, each named in `cancelled`. One already being
    sent to ESPN can't be cancelled and is named in `not_cancelled` (the check before ESPN
    still refuses it unless it was out already). 422 PROTECTION_NOT_ON_ROSTER.
    """
    league_info = await load_owned_league_info(team)
    return respond(await RosterProtectionService.protect(team, league_info, player_id))


@router.delete("/{team_id}/protections/{player_id}", response_model=RosterProtectionsResp)
async def unprotect_player(player_id: int, team: OwnedTeamContext = Depends(get_owned_team)):
    """Unprotect `player_id`: from now on he can be dropped again. Nothing that protecting him
    cancelled comes back. 404 PROTECTION_NOT_FOUND when he isn't protected."""
    return respond(await RosterProtectionService.unprotect(team, player_id))
