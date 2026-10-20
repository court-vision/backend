"""
The app map's routes for its workflow (docs/APP_MAP_PLAN.md in court-vision/docs).
Neither takes a Clerk session: each is behind a bearer of its own.

Sign-in (step 2): the frontend's App Map workflow crawls the signed-in desks
as an account of its own. It presents APP_MAP_TOKEN here and gets a one-time
Clerk sign-in ticket for that account (APP_MAP_CLERK_USER_ID) and no other:
the caller names no user. The crawler's browser redeems the ticket with Clerk
itself, so the workflow never holds Clerk's secret key or the account's
password, and the account is read-only on every route (`core.clerk_auth`).

Upload (step 3): the workflow's last step presents APP_MAP_UPLOAD_TOKEN and
stores the map it crawled for its commit, which CV AI then reads. The body is
read up to a size cap before it is parsed, and a map that isn't the crawler's
shape, or names another build, is refused.
"""

import json

from fastapi import APIRouter, Path, Request, Response, Security
from pydantic import ValidationError

from core.app_map_auth import verify_app_map_token, verify_app_map_upload_token
from core.clerk_auth import create_sign_in_token
from core.errors import AppError, BadRequestError
from core.logging import get_logger
from core.settings import settings
from schemas.app_map import AppMapSignInData, AppMapSignInResp, AppMapUpload, AppMapUploadData, AppMapUploadResp
from schemas.common import ApiStatus
from services.ai import app_map as app_maps

log = get_logger("app_map")

router = APIRouter(prefix="/app-map", tags=["app map"])

# Long enough to start the crawl's browser and redeem it; a ticket left in a
# log afterwards is already spent or expired.
SIGN_IN_TOKEN_TTL_SECONDS = 300


@router.post("/sign-in-token", response_model=AppMapSignInResp, dependencies=[Security(verify_app_map_token)])
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


# The map versions this backend reads (frontend src/lib/app-map/map.ts, APP_MAP_VERSION)
MAP_VERSIONS = {1}
# A signed-out crawl of every desk is about 300 KB
MAX_MAP_BYTES = 4 * 1024 * 1024
BUILD_PATTERN = r"^[0-9a-f]{7,40}$"


async def _body(request: Request) -> bytes:
    """The request body, refused as soon as it passes the cap."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_MAP_BYTES:
        raise AppError("APP_MAP_TOO_LARGE", "The map is over the size cap", status_code=413, log_level="warning")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_MAP_BYTES:
            raise AppError("APP_MAP_TOO_LARGE", "The map is over the size cap", status_code=413, log_level="warning")
    return bytes(body)


@router.put(
    "/maps/{build}",
    response_model=AppMapUploadResp,
    dependencies=[Security(verify_app_map_upload_token)],
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "description": "The crawler's map for this build (frontend src/lib/app-map/map.ts)",
    }}}}},
)
async def upload_app_map(request: Request, build: str = Path(pattern=BUILD_PATTERN)):
    """Store the map the App Map workflow crawled for one commit."""
    body = await _body(request)
    try:
        raw = json.loads(body)
        upload = AppMapUpload.model_validate(raw)
    except (ValueError, ValidationError) as e:
        # Only the first error's location: the message reaches the workflow's log, not a user
        where = ".".join(map(str, e.errors()[0]["loc"])) if isinstance(e, ValidationError) else "not JSON"
        raise BadRequestError("APP_MAP_INVALID", f"Not an app map: {where}", log_level="warning") from None
    if upload.version not in MAP_VERSIONS:
        raise BadRequestError("APP_MAP_VERSION", f"Map version {upload.version} is not one this backend reads",
                              log_level="warning")
    if upload.build != build:
        raise BadRequestError("APP_MAP_BUILD_MISMATCH", "The map is of another build", log_level="warning")

    kept, stats, pruned = await app_maps.store(upload, raw, len(body))
    return AppMapUploadResp(
        status=ApiStatus.SUCCESS,
        message=kept or "Map stored",
        data=AppMapUploadData(build=build, crawled_at=upload.crawled_at, stored=kept is None, stats=stats,
                              pruned=pruned),
    )
