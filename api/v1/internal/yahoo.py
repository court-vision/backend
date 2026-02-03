"""
Yahoo Fantasy Basketball API endpoints.

Handles OAuth 2.0 flow and Yahoo Fantasy API integration.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional

from services.yahoo_service import YahooService
from services.user_sync_service import UserSyncService
from schemas.espn import ValidateLeagueResp, TeamDataResp, TeamDataReq, ValidateLeagueReq
from schemas.common import ApiStatus, BaseResponse, LeagueInfo
from core.clerk_auth import get_current_user
from core.settings import settings


router = APIRouter(prefix="/yahoo", tags=["Yahoo Fantasy"])


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


# ---------------------- Helper Functions ---------------------- #

def _get_user_id(current_user: dict) -> str:
    """Get Clerk user ID from current user dict."""
    return current_user.get("clerk_user_id", "")


# ---------------------- OAuth Endpoints ---------------------- #

@router.get("/authorize", response_model=YahooAuthUrlResponse)
async def yahoo_authorize(current_user: dict = Depends(get_current_user)):
    """
    Initiate Yahoo OAuth flow.

    Returns the Yahoo authorization URL that the frontend should redirect to.
    """
    try:
        if not settings.yahoo_client_id:
            return YahooAuthUrlResponse(
                status=ApiStatus.ERROR,
                message="Yahoo OAuth not configured. Please set YAHOO_CLIENT_ID.",
                auth_url=None
            )

        user_id = _get_user_id(current_user)
        auth_url, state = YahooService.get_auth_url(user_id)

        return YahooAuthUrlResponse(
            status=ApiStatus.SUCCESS,
            message="Authorization URL generated",
            auth_url=auth_url
        )

    except Exception as e:
        return YahooAuthUrlResponse(
            status=ApiStatus.ERROR,
            message=f"Failed to generate authorization URL: {str(e)}",
            auth_url=None
        )


@router.get("/callback")
async def yahoo_callback(
    code: str = Query(..., description="Authorization code from Yahoo"),
    state: str = Query(..., description="State token for CSRF protection"),
    error: Optional[str] = Query(None, description="Error code if authorization failed"),
    error_description: Optional[str] = Query(None, description="Error description")
):
    """
    Handle Yahoo OAuth callback.

    This endpoint is called by Yahoo after the user authorizes the app.
    It exchanges the authorization code for tokens and redirects to the frontend.
    """
    # Handle authorization errors
    if error:
        error_msg = error_description or error
        redirect_url = f"{settings.frontend_url}/manage-teams?yahoo_error={error_msg}"
        return RedirectResponse(url=redirect_url)

    try:
        # Validate state token
        state_data = YahooService.validate_state(state)
        if not state_data:
            redirect_url = f"{settings.frontend_url}/manage-teams?yahoo_error=invalid_state"
            return RedirectResponse(url=redirect_url)

        # Exchange code for tokens
        tokens = await YahooService.exchange_code_for_tokens(code)

        # Store tokens in session or pass to frontend
        # For now, we'll pass tokens via URL params (in production, use secure session)
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        token_expiry = tokens.get("token_expiry", "")

        # Redirect to frontend with success indicator
        # The frontend will then call /yahoo/leagues to get the user's leagues
        redirect_url = (
            f"{settings.frontend_url}/manage-teams"
            f"?yahoo_connected=true"
            f"&yahoo_access_token={access_token}"
            f"&yahoo_refresh_token={refresh_token}"
            f"&yahoo_token_expiry={token_expiry}"
        )
        return RedirectResponse(url=redirect_url)

    except Exception as e:
        redirect_url = f"{settings.frontend_url}/manage-teams?yahoo_error={str(e)}"
        return RedirectResponse(url=redirect_url)


# ---------------------- League/Team Discovery Endpoints ---------------------- #

@router.get("/leagues", response_model=YahooLeaguesResponse)
async def get_user_leagues(
    access_token: str = Query(..., description="Yahoo access token"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all Yahoo fantasy basketball leagues for the authenticated user.

    Call this after OAuth to discover the user's leagues.
    """
    try:
        leagues = await YahooService.get_user_leagues(access_token)

        return YahooLeaguesResponse(
            status=ApiStatus.SUCCESS,
            message=f"Found {len(leagues)} leagues",
            leagues=[YahooLeagueResponse(**league) for league in leagues]
        )

    except Exception as e:
        return YahooLeaguesResponse(
            status=ApiStatus.ERROR,
            message=f"Failed to fetch Yahoo leagues: {str(e)}",
            leagues=None
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
    try:
        teams = await YahooService.get_user_teams(access_token, league_key)

        return YahooTeamsResponse(
            status=ApiStatus.SUCCESS,
            message=f"Found {len(teams)} teams",
            teams=[YahooTeamResponse(**team) for team in teams]
        )

    except Exception as e:
        return YahooTeamsResponse(
            status=ApiStatus.ERROR,
            message=f"Failed to fetch Yahoo teams: {str(e)}",
            teams=None
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
    Refresh an expired Yahoo access token.
    """
    try:
        tokens = await YahooService.refresh_access_token(refresh_token)

        return YahooTokenResponse(
            status=ApiStatus.SUCCESS,
            message="Token refreshed successfully",
            access_token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_expiry=tokens.get("token_expiry")
        )

    except Exception as e:
        return YahooTokenResponse(
            status=ApiStatus.ERROR,
            message=f"Failed to refresh token: {str(e)}",
            access_token=None,
            refresh_token=None,
            token_expiry=None
        )
