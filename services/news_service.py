"""
News and availability reads (nba.news_items and nba.player_availability, migration
0035; docs/NEWS_AND_INJURY_PIPELINES_PLAN.md § 7).

data-platform writes both tables: the news feed (pipelines/news_feed.py) stores
items from ESPN's injury notes and player news, Rotowire and RealGM, linked to
the players and teams they are about; the availability pipeline keeps one
verdict per player and game date. This module is every backend read of them:

- `player_news`: the items about one player, newest first (Scout, the public
  route, Dime's get_player_news).
- `news_feed`: the league's items, filtered by kind, NBA team, players or time
  (the public route, which leaves the body out, and Dime's get_news).
- `latest_item`: a player's newest item, for get_player_status.
- `verdict_for`: a player's verdict for today's game, or his next one.

An item's time is the source's `published_at`, else when the feed fetched it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from peewee import fn

from db.base import run_db
from db.models.nba.availability import PlayerAvailability
from db.models.nba.news import NEWS_KINDS, NewsItem, NewsItemPlayer, NewsItemTeam
from db.models.nba.players import Player

# Dime reads a blurb this long at most: Rotowire's paragraph fits, an ESPN long comment is cut
BODY_CHARS = 300
MAX_LIMIT = 50


def _when():
    return fn.COALESCE(NewsItem.published_at, NewsItem.fetched_at)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Item:
    id: int
    source: str
    kind: str
    headline: str
    body: Optional[str]
    url: Optional[str]
    attribution: Optional[str]
    published_at: Optional[datetime]
    players: tuple[tuple[int, str], ...]     # (NBA player_id, name)
    teams: tuple[str, ...]                   # NBA tricodes

    def view(self, *, body: bool = True, body_chars: Optional[int] = None) -> dict[str, Any]:
        text = self.body if body else None
        if text and body_chars is not None and len(text) > body_chars:
            text = text[:body_chars].rstrip() + "…"
        out: dict[str, Any] = {
            "news_id": self.id,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "source": self.source,
            "kind": self.kind,
            "headline": self.headline,
            "attribution": self.attribution,
            "url": self.url,
            "players": [{"player_id": pid, "name": name} for pid, name in self.players],
            "teams": list(self.teams),
        }
        if body:
            out["body"] = text
        return out


def _items(rows: Sequence[NewsItem]) -> list[Item]:
    """Rows with their players and teams, one query each."""
    ids = [r.id for r in rows]
    players: dict[int, list[tuple[int, str]]] = {}
    teams: dict[int, list[str]] = {}
    if ids:
        for item_id, pid, name in (NewsItemPlayer
                                   .select(NewsItemPlayer.news_item, NewsItemPlayer.player, Player.name)
                                   .join(Player, on=(NewsItemPlayer.player == Player.id))
                                   .where(NewsItemPlayer.news_item.in_(ids))
                                   .order_by(Player.name)
                                   .tuples()):
            players.setdefault(item_id, []).append((pid, name))
        for item_id, team in (NewsItemTeam.select(NewsItemTeam.news_item, NewsItemTeam.team)
                              .where(NewsItemTeam.news_item.in_(ids)).order_by(NewsItemTeam.team).tuples()):
            teams.setdefault(item_id, []).append(team)
    return [Item(id=r.id, source=r.source, kind=r.kind, headline=r.headline, body=r.body, url=r.url,
                 attribution=r.attribution, published_at=_aware(r.published_at or r.fetched_at),
                 players=tuple(players.get(r.id, ())), teams=tuple(teams.get(r.id, ())))
            for r in rows]


def _player_news(player_id: int, since: datetime, limit: int) -> list[Item]:
    rows = list(NewsItem.select(NewsItem)
                .join(NewsItemPlayer, on=(NewsItemPlayer.news_item == NewsItem.id))
                .where(NewsItemPlayer.player == player_id, _when() >= since)
                .order_by(_when().desc(), NewsItem.id.desc())
                .limit(limit))
    return _items(rows)


async def player_news(player_id: int, *, days: int = 7, limit: int = 20,
                      now: Optional[datetime] = None) -> list[Item]:
    now = now or datetime.now(timezone.utc)
    return await run_db("news.player", _player_news, player_id, now - timedelta(days=days),
                        max(1, min(limit, MAX_LIMIT)))


def _feed(since: datetime, kinds: Optional[Sequence[str]], team: Optional[str],
          player_ids: Optional[Sequence[int]], limit: int) -> list[Item]:
    query = NewsItem.select(NewsItem).where(_when() >= since)
    if kinds:
        query = query.where(NewsItem.kind.in_(list(kinds)))
    if team:
        query = query.where(NewsItem.id.in_(
            NewsItemTeam.select(NewsItemTeam.news_item).where(NewsItemTeam.team == team.upper())))
    if player_ids is not None:
        if not player_ids:
            return []
        query = query.where(NewsItem.id.in_(
            NewsItemPlayer.select(NewsItemPlayer.news_item).where(NewsItemPlayer.player.in_(list(player_ids)))))
    rows = list(query.order_by(_when().desc(), NewsItem.id.desc()).limit(limit))
    return _items(rows)


async def news_feed(*, since: Optional[datetime] = None, hours: int = 24, kinds: Optional[Iterable[str]] = None,
                    team: Optional[str] = None, player_ids: Optional[Iterable[int]] = None, limit: int = 20,
                    now: Optional[datetime] = None) -> list[Item]:
    """The league's items since `since` (default the last `hours`), newest first."""
    now = now or datetime.now(timezone.utc)
    start = since or now - timedelta(hours=hours)
    wanted = [k for k in (kinds or []) if k in NEWS_KINDS]
    return await run_db("news.feed", _feed, start, wanted or None, team,
                        None if player_ids is None else sorted(set(player_ids)), max(1, min(limit, MAX_LIMIT)))


def _latest(player_id: int, since: datetime) -> Optional[Item]:
    items = _player_news(player_id, since, 1)
    return items[0] if items else None


async def latest_item(player_id: int, *, days: int = 3, now: Optional[datetime] = None) -> Optional[Item]:
    now = now or datetime.now(timezone.utc)
    return await run_db("news.latest", _latest, player_id, now - timedelta(days=days))


def items_by_id(ids: Sequence[int]) -> dict[int, Item]:
    """Blocking: the items named, with their players (the events route)."""
    if not ids:
        return {}
    rows = list(NewsItem.select().where(NewsItem.id.in_(sorted(set(ids)))))
    return {i.id: i for i in _items(rows)}


# ------------------------------- availability ------------------------------- #


def _verdict(player_id: int, on_or_after: date) -> Optional[PlayerAvailability]:
    return (PlayerAvailability.select()
            .where(PlayerAvailability.player == player_id, PlayerAvailability.game_date >= on_or_after)
            .order_by(PlayerAvailability.game_date)
            .first())


def verdict_view(row: PlayerAvailability) -> dict[str, Any]:
    return {
        "game_date": row.game_date.isoformat() if row.game_date else None,
        "has_game": row.game_id is not None,
        "verdict": row.verdict,
        "final": bool(row.final),
        "decided_by": row.decided_by,
        "reason": row.reason,
        "detail": row.detail,
        "expected_return": row.expected_return.isoformat() if row.expected_return else None,
        "previous_verdict": row.previous_verdict,
        "changed_at": _aware(row.changed_at).isoformat() if row.changed_at else None,
    }


async def verdict_for(player_id: int, today: date) -> Optional[dict[str, Any]]:
    """The player's verdict for `today`'s game, or the next date that has one."""
    row = await run_db("news.verdict", _verdict, player_id, today)
    return verdict_view(row) if row is not None else None
