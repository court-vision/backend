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


class YahooTokenResponse(BaseResponse):
    """Response containing Yahoo OAuth tokens."""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expiry: Optional[str] = None


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

    if not state or not YahooService.validate_state(state):
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

    # Tokens travel to the frontend via URL params (it then calls /yahoo/leagues)
    return _manage_teams_redirect(
        yahoo_connected="true",
        yahoo_access_token=tokens.get("access_token", ""),
        yahoo_refresh_token=tokens.get("refresh_token", ""),
        yahoo_token_expiry=tokens.get("token_expiry", ""),
    )


# ---------------------- League/Team Discovery Endpoints ---------------------- #

@router.get("/leagues", response_model=YahooLeaguesResponse)
async def get_user_leagues(
    access_token: str = Query(..., description="Yahoo access token"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all Yahoo fantasy basketball leagues for the authenticated user.

    Call this after OAuth to discover the user's leagues. A rejected token is a
    403 PROVIDER_AUTH_EXPIRED; a Yahoo outage a 502/504.
    """
    leagues = await YahooService.get_user_leagues(access_token)
    return YahooLeaguesResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(leagues)} leagues",
        leagues=[YahooLeagueResponse(**league) for league in leagues]
    )


@router.get("/teams", response_model=YahooTeamsResponse)
async def get_league_teams(
    access_token: str = Query(..., description="Yahoo access token"),
    league_key: str = Query(..., description="Yahoo league key"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all teams in a specific Yahoo league.

    Use this to let the user select which team they own.
    """
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

@router.post("/refresh_token", response_model=YahooTokenResponse)
async def refresh_token(
    refresh_token: str = Query(..., description="Yahoo refresh token"),
    current_user: dict = Depends(get_current_user)
):
    """
    Refresh an expired Yahoo access token (403 PROVIDER_AUTH_EXPIRED when Yahoo rejects it).
    """
    tokens = await YahooService.refresh_access_token(refresh_token)
    return YahooTokenResponse(
        status=ApiStatus.SUCCESS,
        message="Token refreshed successfully",
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_expiry=tokens.get("token_expiry")
    )
