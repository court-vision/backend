"""
Pydantic schemas for the app map (docs/APP_MAP_PLAN.md in court-vision/docs):
the crawler's one-time sign-in, and the map the workflow uploads.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from schemas.common import ApiModel, BaseResponse


class AppMapSignInData(ApiModel):
    """A one-time Clerk sign-in ticket for the app map's account."""
    ticket: str
    expires_in_seconds: int


class AppMapSignInResp(BaseResponse):
    """POST /v1/internal/app-map/sign-in-token"""
    data: Optional[AppMapSignInData] = None


# ---- The map, as the crawler writes it (frontend src/lib/app-map/map.ts) ----
# Only what the backend reads is checked; anything else in the map is kept as
# uploaded, so the crawler can add a field without a backend release. A field
# that changes meaning bumps the map's `version`, which the upload refuses
# until the backend knows it.

class _MapPart(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class MapStep(_MapPart):
    role: str
    name: str
    example: bool = False
    within: Optional[str] = None


class MapDesk(_MapPart):
    id: str
    label: str
    href: str
    note: str = ""
    keywords: list[str] = []
    needs: Optional[str] = None
    keys: Optional[str] = None


class MapScreen(_MapPart):
    id: str
    desk: str
    title: str
    trail: list[MapStep]
    url: str
    link: Optional[str] = None
    tree: str
    signed_in: bool = Field(False, alias="signedIn")


class MapCopy(_MapPart):
    file: str
    desk: Optional[str] = None
    seen: list[str] = []
    unseen: list[str] = []


class AppMapUpload(_MapPart):
    version: int
    build: str
    crawled_at: datetime = Field(alias="crawledAt")
    desks: list[MapDesk] = Field(min_length=1)
    screens: list[MapScreen] = Field(min_length=1)
    # `copy` would shadow BaseModel.copy
    copy_: list[MapCopy] = Field([], alias="copy")
    findings: list[dict[str, Any]] = []


class AppMapStats(ApiModel):
    screens: int
    signed_in_screens: int
    desks: int
    unseen_copy: int
    findings: int
    bytes: int


class AppMapUploadData(ApiModel):
    build: str
    crawled_at: datetime
    # False when a signed-out crawl met a signed-in map of the same build, which stays
    stored: bool
    stats: AppMapStats
    # Maps crawled over 30 days ago, removed by this upload
    pruned: int


class AppMapUploadResp(BaseResponse):
    """PUT /v1/internal/app-map/maps/{build}"""
    data: Optional[AppMapUploadData] = None
