"""
The app map's sign-in (docs/APP_MAP_PLAN.md in court-vision/docs, step 2).

The frontend's App Map workflow crawls the signed-in desks as an account of
its own. It presents APP_MAP_TOKEN here and gets a one-time Clerk sign-in
ticket for that account (APP_MAP_CLERK_USER_ID) and no other: the caller
names no user. The crawler's browser redeems the ticket with Clerk itself, so
the workflow never holds Clerk's secret key or the account's password, and
the account is read-only on every route (`core.clerk_auth`).
"""

from fastapi import APIRouter, Response, Security

from core.app_map_auth import verify_app_map_token
from core.clerk_auth import create_sign_in_token
from core.logging import get_logger
from core.settings import settings
from schemas.app_map import AppMapSignInData, AppMapSignInResp
from schemas.common import ApiStatus

log = get_logger("app_map")

router = APIRouter(prefix="/app-map", tags=["app map"], dependencies=[Security(verify_app_map_token)])

# Long enough to start the crawl's browser and redeem it; a ticket left in a
# log afterwards is already spent or expired.
SIGN_IN_TOKEN_TTL_SECONDS = 300


@router.post("/sign-in-token", response_model=AppMapSignInResp)
def create_app_map_sign_in_token(response: Response):
    """A one-time sign-in as the app map's account, for the crawl's browser."""
    ticket = create_sign_in_token(settings.app_map_clerk_user_id, SIGN_IN_TOKEN_TTL_SECONDS)
    log.info(
        "app_map_sign_in_token_issued",
        clerk_user_id=settings.app_map_clerk_user_id,
        expires_in_seconds=SIGN_IN_TOKEN_TTL_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"
    return AppMapSignInResp(
        status=ApiStatus.SUCCESS,
        message="Sign-in token created",
        data=AppMapSignInData(ticket=ticket, expires_in_seconds=SIGN_IN_TOKEN_TTL_SECONDS),
    )
