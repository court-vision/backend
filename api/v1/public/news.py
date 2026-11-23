"""GET /v1/news: the league's news feed (headlines, no blurbs; services/news_service.py)."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, Request

from core.rate_limit import PUBLIC_RATE_LIMIT, limiter
from core.responses import respond
from schemas.common import ApiStatus
from schemas.news import NewsItemResp, NewsKind, NewsListData, NewsListResp
from services import news_service

router = APIRouter(prefix="/news", tags=["News"])


@router.get(
    "",
    response_model=NewsListResp,
    summary="League news",
    description=(
        "The newest NBA news items Court Vision has stored, from ESPN, Rotowire and RealGM: headline, source, "
        "attribution, link and the players and teams each is about. No blurb: a player's own news "
        "(`/players/{player_id}/news`) carries it."
    ),
    responses={200: {"description": "News retrieved"}, 429: {"description": "Rate limit exceeded"}},
)
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_news(
    request: Request,
    kind: Optional[list[NewsKind]] = Query(None, description="Only these kinds"),
    team: Optional[str] = Query(None, pattern=r"^[A-Za-z]{2,3}$", description="An NBA tricode"),
    since: Optional[datetime] = Query(None, description="Only items since this time (default the last 24 hours)"),
    limit: int = Query(20, ge=1, le=50),
) -> NewsListResp:
    items = await news_service.news_feed(since=since, kinds=kind, team=team, limit=limit)
    return respond(NewsListResp(
        status=ApiStatus.SUCCESS,
        message=f"{len(items)} item(s)",
        data=NewsListData(items=[NewsItemResp(**i.view(body=False)) for i in items]),
    ))
