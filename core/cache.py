"""In-process cache for rendered public responses.

Holds the *serialized* body keyed by the query that produced it, so a hit costs
neither the database query nor the pydantic-build-and-serialise pass. For
rankings that is the whole of the server-side work: the measurements in
docs/PRODUCTION_READINESS.md put SQL at 40% of it and Python at 60%, so caching
the rendered bytes is the only lever that removes both.

Design notes:

- **TTL, not writer invalidation.** The data changes once a day, after the
  post-game pipelines run — in a different service, against the same database.
  A short TTL bounds staleness without a cross-service purge call (and its
  failure modes); flipping to explicit invalidation later only means calling
  `clear()` from an internal route.
- **Single-flight.** Without it, expiry under load sends every concurrent
  request to the database at once — exactly the stampede the cache exists to
  prevent. Followers wait on the leader's render and share its result (or its
  exception).
- **Bounded by entry count.** Part of the key space (`categories`, `min_games`)
  is caller-controlled, so an unbounded dict is a memory-growth surface. A
  rankings body is ~120 KB (points) to ~350 KB (categories), which is what the
  default entry ceiling is sized against.
- **Per process.** Each replica keeps its own copy; entries do not survive a
  restart. That is fine for a read-through cache and is the same trade-off the
  rate limiter and OAuth state already make (see PRODUCTION_READINESS item 7).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Hashable, Optional

from core.logging import get_logger

log = get_logger("cache")

CacheKey = Hashable


@dataclass(frozen=True)
class CachedResponse:
    """A rendered response body plus what the HTTP layer needs to serve it."""

    body: bytes
    etag: str
    stored_at: float

    @classmethod
    def of(cls, body: bytes) -> "CachedResponse":
        # blake2b over the body: identical data renders to an identical tag on
        # every replica, so a client's If-None-Match survives being reassigned.
        digest = hashlib.blake2b(body, digest_size=16).hexdigest()
        return cls(body=body, etag=f'"{digest}"', stored_at=time.monotonic())

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.stored_at)


class ResponseCache:
    """TTL + LRU cache of rendered bodies, with single-flight on misses."""

    def __init__(self, name: str, ttl_seconds: float, max_entries: int):
        self.name = name
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._entries: "OrderedDict[CacheKey, CachedResponse]" = OrderedDict()
        self._inflight: dict[CacheKey, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    @property
    def enabled(self) -> bool:
        """False disables caching entirely — the kill switch is a TTL of 0."""
        return self.ttl_seconds > 0 and self.max_entries > 0

    def get(self, key: CacheKey) -> Optional[CachedResponse]:
        """The live entry for `key`, or None when absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.age_seconds() >= self.ttl_seconds:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry

    async def get_or_render(
        self,
        key: CacheKey,
        render: Callable[[], Awaitable[bytes]],
    ) -> CachedResponse:
        """Return the cached body for `key`, rendering it once if needed.

        `render` may raise to opt a result out of the cache (an error envelope,
        say); the exception reaches this caller and every coalesced follower,
        and nothing is stored.
        """
        if not self.enabled:
            return CachedResponse.of(await render())

        entry = self.get(key)
        if entry is not None:
            self.hits += 1
            return entry

        inflight = self._inflight.get(key)
        if inflight is not None:
            self.coalesced += 1
            # Shielded: a follower giving up must not cancel the shared render.
            return await asyncio.shield(inflight)

        self.misses += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # Nobody may be waiting when this fails; retrieve the exception here so
        # asyncio does not log it as never-retrieved. Followers still see it.
        future.add_done_callback(_swallow_exception)
        self._inflight[key] = future
        try:
            body = await render()
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            entry = CachedResponse.of(body)
            self._put(key, entry)
            if not future.done():
                future.set_result(entry)
            return entry
        finally:
            self._inflight.pop(key, None)

    def clear(self) -> None:
        """Drop every entry (tests, and a future explicit-invalidation route)."""
        self._entries.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
        }

    def _put(self, key: CacheKey, entry: CachedResponse) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            evicted, _ = self._entries.popitem(last=False)
            log.debug("cache_evicted", cache=self.name, key=str(evicted))


def _swallow_exception(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
