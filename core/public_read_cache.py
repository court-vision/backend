"""Rendered-response caches for the public reads Scout and Week make.

The same mechanism as rankings (core/cache.py stores the bytes, core/http_cache.py
answers with an ETag, a 304 and `Cache-Control: public`), applied to the player
and team sheets, games and schedule weeks. Before this, only rankings sent cache
headers, so every reload or revisit of a Scout sheet re-paid each round trip
(about 100 ms per request from the US West Coast) for data that changes a few
times a day, and every player open re-ran the league-wide percentile aggregate.

TTL follows how often the data behind a route can change:

- **300 s** for player and team data. The pipelines rewrite it once a day after
  the post-game run (team schedules also move as games go final), so a replica
  lags a pipeline write by at most five minutes. A client is never told to hold
  its copy longer than the replica's own entry has left. Percentiles lag by up
  to ten: they are read off a league pool aggregated once per ten-minute
  window (services/player_service.py), and their entries are keyed by that
  window and never held by a client past its end.
- **3600 s** for games two or more NBA nights old: final, and only a pipeline
  re-run or a correction would ever change them. Last night's slate stays on
  300 s: the post-game run that writes its final scores can still be retrying
  after the 6 AM ET date flip.
- **3600 s** for schedule weeks, keyed by the fantasy day and never held by a
  client past the 3 AM ET rollover that moves `current_week` and `season_day`.

Never cached: today's games (the live scoreboard overlay), injury status,
ownership, the live endpoints, and anything resolved from the caller.

Memory: every cache is bounded by entries and by bytes. Bodies measured in
production on 2026-10-13: a full-season game log 13.6 KB (about 17 KB at 82
games), an 82-game team schedule 13.1 KB, a roster 4.1 KB, a slate 1.4-2.5 KB,
schedule weeks 2.2 KB, and 0.2-0.6 KB for profile, trends, projection and
percentiles. The byte budgets below add up to 22.3 MB, the most this module can
ever hold however a caller varies the keys.
"""

from __future__ import annotations

from core.cache import ResponseCache
from core.settings import settings

KB = 1024
MB = 1024 * KB

# Daily pipeline data: player and team sheets, recent and upcoming slates.
DAILY_TTL_SECONDS = 300.0
# Settled data: games two or more nights old, and the static fantasy calendar.
SETTLED_TTL_SECONDS = 3600.0


def _cache(name: str, ttl_seconds: float, max_entries: int, max_bytes: int) -> ResponseCache:
    # A TTL of 0 disables a ResponseCache; the routes then render uncached.
    ttl = ttl_seconds if settings.public_read_cache_enabled else 0.0
    return ResponseCache(f"public.{name}", ttl_seconds=ttl, max_entries=max_entries, max_bytes=max_bytes)


# Players. Stats are shared by /players/stats and /players/{id}/stats (same
# service, same key shape).
PLAYER_STATS = _cache("players.stats", DAILY_TTL_SECONDS, 1024, 8 * MB)
PLAYER_PROFILE = _cache("players.profile", DAILY_TTL_SECONDS, 2048, 2 * MB)
PLAYER_PROJECTION = _cache("players.projection", DAILY_TTL_SECONDS, 2048, 1 * MB)
PLAYER_PERCENTILES = _cache("players.percentiles", DAILY_TTL_SECONDS, 2048, 1 * MB)
PLAYER_TRENDS = _cache("players.trends", DAILY_TTL_SECONDS, 2048, 1 * MB)

# Teams: 30 of them, times the schedule's `upcoming`/`limit` variants.
TEAM_SCHEDULE = _cache("teams.schedule", DAILY_TTL_SECONDS, 256, 4 * MB)
TEAM_STATS = _cache("teams.stats", DAILY_TTL_SECONDS, 64, 256 * KB)
TEAM_ROSTER = _cache("teams.roster", DAILY_TTL_SECONDS, 64, 1 * MB)

# Games by date: last night and the days ahead, and the settled past (the table
# goes back to 2012-13, so that key space is the larger one).
GAMES = _cache("games.recent", DAILY_TTL_SECONDS, 256, 1 * MB)
GAMES_SETTLED = _cache("games.settled", SETTLED_TTL_SECONDS, 1024, 3 * MB)

SCHEDULE_WEEKS = _cache("schedule.weeks", SETTLED_TTL_SECONDS, 8, 64 * KB)

ALL_CACHES: tuple[ResponseCache, ...] = (
    PLAYER_STATS,
    PLAYER_PROFILE,
    PLAYER_PROJECTION,
    PLAYER_PERCENTILES,
    PLAYER_TRENDS,
    TEAM_SCHEDULE,
    TEAM_STATS,
    TEAM_ROSTER,
    GAMES,
    GAMES_SETTLED,
    SCHEDULE_WEEKS,
)


def key(*parts) -> tuple:
    """A cache key: every request input that shapes the body, plus the active season.

    Most of these services read the season from settings rather than the
    request (to scope game logs, pick the calendar, default a projection), so
    it is part of every key: an entry can never answer for another season.
    """
    return (*parts, settings.nba_season)


def clear_all() -> None:
    """Drop every entry (tests, and a future explicit-invalidation route)."""
    for cache in ALL_CACHES:
        cache.clear()
