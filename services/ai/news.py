"""
Dime's news tools (the Dime autopilot plan, step 6; docs/NEWS_AND_INJURY_PIPELINES_PLAN.md
§ 7): what a manager reads about players before a move, from the items data-platform's
news feed stores (ESPN's injury notes and player news, Rotowire, RealGM).

- `get_player_news`: one player's items, newest first, each with its source, time,
  headline and blurb.
- `get_news`: the league's items over a window, or only those about one of the
  user's rosters or one NBA team, by kind.

Both are reads (services/news_service.py). An item is quoted with its source and
time; a number in one is the source's claim, not a stat. The tool definitions are
appended to `AGENT_TOOLS` after the check-ins' and never reordered: the list is a
cached prefix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services import news_service

GET_PLAYER_NEWS_NAME = "get_player_news"
GET_NEWS_NAME = "get_news"
NEWS_TOOL_NAMES = frozenset({GET_PLAYER_NEWS_NAME, GET_NEWS_NAME})

NewsKind = Literal["injury", "availability", "lineup", "transaction", "performance", "rumor", "other"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlayerNewsInput(_Input):
    player_id: int = Field(..., gt=0, lt=2**31)
    days: int = Field(7, ge=1, le=30)
    limit: int = Field(8, ge=1, le=20)


class NewsInput(_Input):
    team_id: Optional[int] = Field(None, gt=0, lt=2**31)
    nba_team: Optional[str] = Field(None, pattern=r"^[A-Za-z]{2,3}$")
    kinds: list[NewsKind] = Field(default_factory=list, max_length=7)
    hours: int = Field(24, ge=1, le=168)
    limit: int = Field(15, ge=1, le=30)


_ITEM_SHAPE = (
    "Each item: news_id, published_at (UTC), source (espn_injuries, espn_player_news, rotowire_rss, "
    "realgm_rss), kind (injury, availability, lineup, transaction, performance, rumor, other), headline, "
)

GET_PLAYER_NEWS: dict[str, Any] = {
    "name": GET_PLAYER_NEWS_NAME,
    "description": (
        "The news about one player, newest first, from the feeds Court Vision reads: ESPN's injury notes "
        "and player news, Rotowire and RealGM. " + _ITEM_SHAPE + "body (the blurb, cut short), attribution "
        "(the reporter, when named), url, and the players and NBA teams it is about. Quote news with its "
        "source and time. A number in a blurb is the source's claim: a stat you state still comes from a stats "
        "tool. An empty list means nothing stored in the window, not that nothing happened."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "player_id": {"type": "integer", "description": "NBA player_id"},
            "days": {"type": "integer", "description": "How far back, 1 to 30 days (default 7)"},
            "limit": {"type": "integer", "description": "At most this many items, 1 to 20 (default 8)"},
        },
        "required": ["player_id"],
        "additionalProperties": False,
    },
}

GET_NEWS: dict[str, Any] = {
    "name": GET_NEWS_NAME,
    "description": (
        "The league's news over the last hours, newest first: all of it, or only items about one of the "
        "user's rosters (team_id) or one NBA team (nba_team, a tricode like NYK), of the kinds named. "
        + _ITEM_SHAPE + "attribution, url, and the players and NBA teams it is about; no blurb (read one "
        "player's with get_player_news). Use it for what happened today, or before a check-in's calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids: only news about its roster"},
            "nba_team": {"type": "string", "description": "An NBA tricode: only news about that team"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": [
                "injury", "availability", "lineup", "transaction", "performance", "rumor", "other"]},
                      "description": "Only these kinds (default all)"},
            "hours": {"type": "integer", "description": "How far back, 1 to 168 hours (default 24)"},
            "limit": {"type": "integer", "description": "At most this many items, 1 to 30 (default 15)"},
        },
        "additionalProperties": False,
    },
}

NEWS_TOOLS: list[dict[str, Any]] = [GET_PLAYER_NEWS, GET_NEWS]


async def get_player_news(args: PlayerNewsInput, now: Optional[datetime] = None) -> dict[str, Any]:
    items = await news_service.player_news(args.player_id, days=args.days, limit=args.limit, now=now)
    return {
        "player_id": args.player_id,
        "days": args.days,
        "items": [i.view(body_chars=news_service.BODY_CHARS) for i in items],
    }


async def get_news(args: NewsInput, roster: Optional[frozenset[int]] = None,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """`roster` is the team's players when `team_id` was given (the caller reads and owns it)."""
    now = now or datetime.now(timezone.utc)
    if args.team_id is not None and roster is None:
        return {"hours": args.hours, "team_id": args.team_id, "items": [],
                "note": "The roster couldn't be read from ESPN just now; try again, or name an nba_team."}
    items = await news_service.news_feed(hours=args.hours, kinds=args.kinds, team=args.nba_team,
                                         player_ids=roster if args.team_id is not None else None,
                                         limit=args.limit, now=now)
    out: dict[str, Any] = {
        "hours": args.hours,
        "items": [i.view(body=False) for i in items],
    }
    if args.team_id is not None:
        out["team_id"] = args.team_id
    return out
