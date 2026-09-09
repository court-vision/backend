"""
Add/drop a player on ESPN for a saved team — the write behind the streamers
page's "pick him up". Clerk-authenticated; ownership via `get_owned_team`;
credentials hydrated only here, at the provider boundary.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.roster_transaction import RosterTransactionReq, RosterTransactionResp
from services.roster_transaction_service import RosterTransactionService

router = APIRouter(prefix="/teams", tags=["roster transactions"])


@router.post("/{team_id}/roster/transactions", response_model=RosterTransactionResp)
async def apply_roster_transaction(req: RosterTransactionReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """
    Pick up and/or release one player as a single ESPN transaction, then return
    the re-read board.

    Failures keep the lineup editor's statuses — 403 ROSTER_WRITE_DISABLED, 409
    ROSTER_WRITE_BLOCKED / ROSTER_STALE (with the fresh board) /
    ROSTER_WRITE_REJECTED (ESPN's own sentence), 503 ROSTER_WRITE_UNAVAILABLE —
    plus 422 ROSTER_TRANSACTION_INVALID with `data.reason` when the board or the
    player pool refuses the request before ESPN is asked (a locked drop, a player
    on waivers, one already rostered, ...). Roster limits are ESPN's call.
    """
    league_info = await load_owned_league_info(team)
    return respond(await RosterTransactionService.apply(team, league_info, req))
