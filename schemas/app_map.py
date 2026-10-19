"""
Pydantic schemas for the app map's sign-in (docs/APP_MAP_PLAN.md in court-vision/docs).
"""

from typing import Optional

from schemas.common import ApiModel, BaseResponse


class AppMapSignInData(ApiModel):
    """A one-time Clerk sign-in ticket for the app map's account."""
    ticket: str
    expires_in_seconds: int


class AppMapSignInResp(BaseResponse):
    """POST /v1/internal/app-map/sign-in-token"""
    data: Optional[AppMapSignInData] = None
