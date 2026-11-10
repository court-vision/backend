"""
Public news reads (GET /v1/players/{player_id}/news, GET /v1/news; services/news_service.py,
docs/NEWS_AND_INJURY_PIPELINES_PLAN.md § 7). Items come from data-platform's news feed: ESPN's
injury notes and player news, Rotowire and RealGM. The league feed carries no blurb (the plan's
decision 10: headline, source, attribution and link only); a player's own news carries it.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

NewsKind = Literal["injury", "availability", "lineup", "transaction", "performance", "rumor", "other"]


class NewsPlayerRef(ApiModel):
    player_id: int = Field(..., description="NBA player id")
    name: str


class NewsItemResp(ApiModel):
    news_id: int
    published_at: Optional[datetime] = Field(None, description="The source's time, else when it was fetched")
    source: str = Field(..., description="espn_injuries, espn_player_news, rotowire_rss or realgm_rss")
    kind: NewsKind
    headline: str
    body: Optional[str] = Field(None, description="The blurb (a player's news only)")
    attribution: Optional[str] = None
    url: Optional[str] = None
    players: list[NewsPlayerRef] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list, description="NBA tricodes")


class NewsListData(ApiModel):
    items: list[NewsItemResp]


class NewsListResp(BaseResponse):
    data: Optional[NewsListData] = None
