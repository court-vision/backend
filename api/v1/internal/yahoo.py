"""
Yahoo Fantasy Basketball API endpoints.

Handles the OAuth 2.0 flow and Yahoo Fantasy API integration. Provider failures
raise `core.errors` AppErrors — 403 PROVIDER_AUTH_EXPIRED for a rejected token,
502/504 for a Yahoo outage, 503 YAHOO_NOT_CONFIGURED without client credentials —
and render through the global handlers. The OAuth callback is the one exception:
the browser is mid-redirect, so it sends the user back to Manage Teams with
`?yahoo_error=oauth_failed` and logs the cause instead of rendering an error body.
"""

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.clerk_auth import get_current_user
from core.logging import get_logger
from core.settings import settings
from schemas.common import ApiStatus, BaseResponse
from schemas.espn import TeamDataReq, TeamDataResp, ValidateLeagueReq, ValidateLeagueResp
from services.yahoo_service import YahooService
from services import credential_service
from services.user_sync_service import UserSyncService
from api.deps import UserContext, get_db_user
from core.errors import BadRequestError
from db.base import run_db

router = APIRouter(prefix="/yahoo", tags=["Yahoo Fantasy"])
log = get_logger("yahoo_api")


# ---------------------- Request/Response Models ---------------------- #

class YahooAuthUrlResponse(BaseResponse):
    """Response containing Yahoo OAuth authorization URL."""
    auth_url: Optional[str] = None


class YahooLeagueResponse(BaseModel):
    """Yahoo league information."""
    league_key: str
    league_id: str
    name: str
    season: str
    num_teams: int
    scoring_type: str


class YahooTeamResponse(BaseModel):
    """Yahoo team information."""
    team_key: str
    team_id: str
    name: str
    is_owned_by_current_login: bool


class YahooLeaguesResponse(BaseResponse):
    """Response containing user's Yahoo leagues."""
    leagues: Optional[list[YahooLeagueResponse]] = None


class YahooTeamsResponse(BaseResponse):
    """Response containing teams in a Yahoo league."""
    teams: Optional[list[YahooTeamResponse]] = None


def _manage_teams_redirect(**params: str) -> RedirectResponse:
    """Redirect to the frontend's Manage Teams page with URL-encoded query params."""
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return RedirectResponse(url=f"{settings.frontend_url}/manage-teams?{query}")


# ---------------------- OAuth Endpoints ---------------------- #

@router.get("/authorize", response_model=YahooAuthUrlResponse)
async def yahoo_authorize(current_user: dict = Depends(get_current_user)):
    """
    Initiate Yahoo OAuth flow.

    Returns the Yahoo authorization URL that the frontend should redirect to
    (503 YAHOO_NOT_CONFIGURED when the client credentials are missing).
    """
    # Yahoo's OAuth state is keyed by the Clerk user id string, not usr.user_id
    auth_url, _state = YahooService.get_auth_url(current_user.get("clerk_user_id", ""))
    return YahooAuthUrlResponse(
        status=ApiStatus.SUCCESS,
        message="Authorization URL generated",
        auth_url=auth_url
    )


@router.get("/callback")
async def yahoo_callback(
    code: Optional[str] = Query(None, description="Authorization code from Yahoo"),
    state: Optional[str] = Query(None, description="State token for CSRF protection"),
    error: Optional[str] = Query(None, description="Error code if authorization failed"),
    error_description: Optional[str] = Query(None, description="Error description")
):
    """
    Handle Yahoo OAuth callback.

    This endpoint is called by Yahoo after the user authorizes the app.
    It exchanges the authorization code for tokens and redirects to the frontend.
    """
    # The user declined, or Yahoo could not authorize
    if error:
        log.info("yahoo_oauth_denied", error=error)
        return _manage_teams_redirect(yahoo_error=error_description or error)

    state_data = YahooService.validate_state(state) if state else None
    if not state_data:
        log.warning("yahoo_oauth_invalid_state")
        return _manage_teams_redirect(yahoo_error="invalid_state")
    if not code:
        log.warning("yahoo_oauth_missing_code")
        return _manage_teams_redirect(yahoo_error="oauth_failed")

    try:
        tokens = await YahooService.exchange_code_for_tokens(code)
    except Exception as exc:  # mid-redirect: never render an error body, never echo the cause
        log.warning("yahoo_oauth_exchange_failed", error=type(exc).__name__,
                    error_code=getattr(exc, "error_code", None))
        return _manage_teams_redirect(yahoo_error="oauth_failed")

    # Tokens are stored server-side and the redirect carries only an opaque,
    # user-scoped connection id. They used to travel as query parameters, which
    # put long-lived Yahoo refresh tokens into browser history, the Referer
    # header, and every proxy and access log on the path.
    # `state_data["user_id"]` is the Clerk id — get_auth_url is called with
    # clerk_user_id, not the numeric usr.users key.
    connection_id = await run_db(
        "yahoo.store_tokens", _store_tokens_for_clerk_user,
        state_data.get("user_id"), "yahoo",
        {
            "yahoo_access_token": tokens.get("access_token", ""),
            "yahoo_refresh_token": tokens.get("refresh_token", ""),
            "yahoo_token_expiry": tokens.get("token_expiry", ""),
        },
    )
    if connection_id is None:
        # CREDENTIAL_KEYS unset: nothing can be stored, so the flow cannot
        # complete without putting tokens in the URL. Refuse rather than regress.
        log.error("yahoo_oauth_store_unavailable")
        return _manage_teams_redirect(yahoo_error="oauth_storage_unavailable")

    return _manage_teams_redirect(
        yahoo_connected="true",
        yahoo_connection=str(connection_id),
    )


def _store_tokens_for_clerk_user(clerk_user_id: str, provider: str, secrets: dict):
    """Resolve the Clerk id to the local user, then store the tokens under it."""
    user = UserSyncService.get_or_create_user(clerk_user_id)
    return credential_service.store_provider_tokens(user.user_id, provider, secrets)


async def _access_token_for(user_id: int, connection_id: int) -> str:
    """Resolve an opaque connection id to its access token, scoped to the owner.

    A connection id is a small integer, so ownership is the access control: a
    row belonging to someone else resolves to nothing, not to their token.
    """
    secrets = await run_db(
        "yahoo.load_tokens", credential_service.load_provider_tokens, user_id, connection_id
    )
    token = (secrets or {}).get("yahoo_access_token")
    if not token:
        raise BadRequestError("YAHOO_CONNECTION_NOT_FOUND",
                              "Yahoo connection not found; reconnect your account")
    return token


# ---------------------- League/Team Discovery Endpoints ---------------------- #

@router.get("/leagues", response_model=YahooLeaguesResponse)
async def get_user_leagues(
    connection_id: int = Query(..., description="Opaque Yahoo connection id from the OAuth callback"),
    user: UserContext = Depends(get_db_user),
):
    """
    Get all Yahoo fantasy basketball leagues for the authenticated user.

    Takes the connection id issued by the callback rather than the access token
    itself, so no Yahoo credential ever appears in a URL. A rejected token is a
    403 PROVIDER_AUTH_EXPIRED; a Yahoo outage a 502/504.
    """
    access_token = await _access_token_for(user.user_id, connection_id)
    leagues = await YahooService.get_user_leagues(access_token)
    return YahooLeaguesResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(leagues)} leagues",
        leagues=[YahooLeagueResponse(**league) for league in leagues]
    )


@router.get("/teams", response_model=YahooTeamsResponse)
async def get_league_teams(
    connection_id: int = Query(..., description="Opaque Yahoo connection id from the OAuth callback"),
    league_key: str = Query(..., description="Yahoo league key"),
    user: UserContext = Depends(get_db_user),
):
    """
    Get all teams in a specific Yahoo league.

    Use this to let the user select which team they own.
    """
    access_token = await _access_token_for(user.user_id, connection_id)
    teams = await YahooService.get_user_teams(access_token, league_key)
    return YahooTeamsResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(teams)} teams",
        teams=[YahooTeamResponse(**team) for team in teams]
    )


# ---------------------- Validation Endpoints ---------------------- #

@router.post("/validate_league", response_model=ValidateLeagueResp)
async def validate_yahoo_league(req: ValidateLeagueReq):
    """
    Validate Yahoo league credentials.

    Checks if the provided credentials can access the specified team.
    """
    return await YahooService.check_league(req.league_info)


# ---------------------- Data Endpoints ---------------------- #

@router.post("/get_roster_data", response_model=TeamDataResp)
async def get_roster_data(
    req: TeamDataReq,
    current_user: dict = Depends(get_current_user)
):
    """
    Get roster data for a Yahoo team.
    """
    return await YahooService.get_team_data(req.league_info, req.fa_count)


@router.post("/get_freeagent_data", response_model=TeamDataResp)
async def get_free_agents(
    req: TeamDataReq,
    current_user: dict = Depends(get_current_user)
):
    """
    Get available free agents from a Yahoo league.
    """
    return await YahooService.get_free_agents(req.league_info, req.fa_count)


# ---------------------- Token Management ---------------------- #
#
# `POST /refresh_token` was removed on 2026-08-28. It took a Yahoo refresh token
# — the longest-lived credential in the system — as a *query parameter*, and
# returned a fresh access and refresh token in the body. Nothing called it: token
# refresh happens inside YahooService._ensure_valid_token during ordinary calls,
# and the refreshed pair is persisted through credential_service. It was pure
# attack surface.
