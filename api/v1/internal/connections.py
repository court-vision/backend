"""
Provider connections: the accounts a user has connected, independent of teams.

ESPN has no OAuth, so its "connection" is the espn_s2/SWID pair -- stored once
per ESPN account and shared by every team on it. Refreshing expired cookies is
one POST here, a new team on an account already connected needs no cookies at
all (TeamService._resolve_connection_handle), and the account's teams can be
listed straight from ESPN to pick from. Responses never carry a credential.
"""

from fastapi import APIRouter, Depends

from api.deps import UserContext, get_db_user
from core.responses import respond
from schemas.connections import (
    EspnAccountTeamsResp,
    EspnConnectReq,
    ProviderConnectionDeleteResp,
    ProviderConnectionListResp,
    ProviderConnectionResp,
)
from services.connection_service import ConnectionService

router = APIRouter(prefix="/connections", tags=["provider connections"])


@router.get("/", response_model=ProviderConnectionListResp)
async def list_connections(user: UserContext = Depends(get_db_user)):
    return respond(await ConnectionService.list_for_user(user.user_id))


@router.post("/espn", response_model=ProviderConnectionResp)
async def connect_espn(req: EspnConnectReq, user: UserContext = Depends(get_db_user)):
    """Connect an ESPN account, or refresh the cookies of one already connected.

    ESPN checks the pair first, by reading one of the account's private leagues
    with it: 403 PROVIDER_AUTH_EXPIRED when it refuses them, 400
    ESPN_ACCOUNT_NOT_FOUND for a SWID it does not know, and nothing is saved.
    An account with only public leagues cannot confirm a pair; it is saved with
    status "unknown".
    """
    return respond(await ConnectionService.connect_espn(user.user_id, req.espn_s2, req.swid))


@router.post("/{connection_id}/verify", response_model=ProviderConnectionResp)
async def verify_connection(connection_id: int, user: UserContext = Depends(get_db_user)):
    """Re-check a stored ESPN connection's cookies. A refusal is a 200 whose
    connection reads "expired"; an ESPN outage, or an account with no private
    league to check against, records nothing."""
    return respond(await ConnectionService.verify(user.user_id, connection_id))


@router.get("/{connection_id}/espn/teams", response_model=EspnAccountTeamsResp)
async def espn_account_teams(connection_id: int, user: UserContext = Depends(get_db_user)):
    """The fantasy basketball teams on a connected ESPN account, as ESPN lists
    them, each marked with the Court Vision team already tracking it."""
    return respond(await ConnectionService.espn_teams(user.user_id, connection_id))


@router.delete("/{connection_id}", response_model=ProviderConnectionDeleteResp)
async def delete_connection(connection_id: int, user: UserContext = Depends(get_db_user)):
    """Remove a connection. Its teams are unlinked, not deleted."""
    return respond(await ConnectionService.delete(user.user_id, connection_id))
