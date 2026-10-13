"""In-process TTL cache for values built on worker threads.

core/cache.py's ResponseCache coalesces misses on the event loop with an
asyncio future. Some expensive reads are synchronous and already run on an
executor thread (inside `run_db`), where there is no loop to await on: the
category value pool is the first (services.player_value_service). This is the
same design for that world — TTL, single-flight, a bounded entry count — with a
`threading.Lock` and an `Event` in place of the future.

Design notes:

- **TTL, not writer invalidation**, for the same reason as ResponseCache: the
  data is written once a day by data-platform, a different service.
- **Single-flight.** Callers run on up to `db_max_in_flight` DB threads at once,
  and a category Week open fires 10-13 valuations together. Without coalescing,
  an expiry under that burst rebuilds the same value on every thread at once,
  each holding a DB permit while it does. Followers wait for the leader's build
  and share its result.
- **A failed build is not shared.** The leader's caller gets the exception; each
  follower then builds for itself, exactly as it would without the cache, rather
  than re-raising one exception object on several threads.
- **A follower's wait is bounded.** Each follower holds a DB permit and a
  connection while it waits, so a stuck leader would stall every caller of its
  key. After `wait_timeout_seconds` a follower stops waiting, detaches the stuck
  build (new callers start a fresh one; its late result is not stored) and
  builds for itself — again exactly the uncached behaviour.
- **Values are shared, so they must be read-only.** Every hit returns the same
  object to every caller on every thread. Store immutable shapes (tuples,
  frozen dataclasses, MappingProxyType) or copy on the way out.
- **Per process**, like every other in-process cache here: each replica keeps
  its own copy and nothing survives a restart.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, TypeVar

from core.logging import get_logger

log = get_logger("cache")

T = TypeVar("T")

# How long a follower waits for the leader's build before building for itself.
# A healthy category pool build takes ~50 ms alone and under 0.5 s inside a Week
# open's burst, so this only ever fires for a leader that is stuck: one query
# held to the 30 s statement_timeout, or a half-open socket, which the
# keepalives take ~60 s to notice. Waiting no longer than one of the leader's
# queries may run keeps a follower's worst case near the uncached one.
DEFAULT_WAIT_TIMEOUT_SECONDS = 30.0


class _Flight:
    """One in-progress build that followers wait on."""

    __slots__ = ("done", "value", "failed", "detached")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.failed = False
        # Set (under the cache's lock) once new callers stop joining this build:
        # a clear() after it started, or a follower that gave up waiting on it.
        # Its result still reaches the callers already waiting, but is never
        # stored, so nothing from before a clear is served after it.
        self.detached = False


class SyncTTLCache:
    """TTL + LRU cache of built values, with single-flight on misses, for threads."""

    def __init__(self, name: str, ttl_seconds: float, max_entries: int,
                 clock: Callable[[], float] = time.monotonic,
                 wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS):
        self.name = name
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self._inflight: dict[Hashable, _Flight] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0
        self.wait_timeouts = 0

    @property
    def enabled(self) -> bool:
        """False bypasses the cache entirely — the kill switch is a TTL of 0."""
        return self.ttl_seconds > 0 and self.max_entries > 0

    def get_or_build(self, key: Hashable, build: Callable[[], T]) -> T:
        """Return the live value for `key`, building it once if needed.

        `build` runs on the calling thread, outside the lock, and must not ask
        this cache for its own key (that would wait on itself).
        """
        if not self.enabled:
            return build()

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                stored_at, value = entry
                if self._clock() - stored_at < self.ttl_seconds:
                    self._entries.move_to_end(key)
                    self.hits += 1
                    return value
                del self._entries[key]
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = self._inflight[key] = _Flight()
                self.misses += 1
            else:
                self.coalesced += 1

        if not leader:
            return self._follow(key, flight, build)

        try:
            value = build()
        except BaseException:
            flight.failed = True
            raise
        else:
            flight.value = value
            with self._lock:
                if not flight.detached:
                    self._put(key, value)
            return value
        finally:
            with self._lock:
                if self._inflight.get(key) is flight:
                    del self._inflight[key]
            flight.done.set()

    def clear(self) -> None:
        """Drop every entry (tests, and a future explicit-invalidation route).

        Nothing built before a clear is served after it: builds in flight finish
        for the callers already waiting on them, but are not stored, and a new
        caller starts a build of its own rather than joining one of them."""
        with self._lock:
            self._entries.clear()
            for flight in self._inflight.values():
                flight.detached = True
            self._inflight.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "coalesced": self.coalesced,
                "wait_timeouts": self.wait_timeouts,
            }

    def _follow(self, key: Hashable, flight: _Flight, build: Callable[[], T]) -> T:
        """Wait for the leader's build and share it; build alone when it fails or hangs."""
        if not flight.done.wait(self.wait_timeout_seconds):
            with self._lock:
                self.wait_timeouts += 1
                # Later callers start a fresh build instead of queueing behind this one.
                if self._inflight.get(key) is flight:
                    del self._inflight[key]
                flight.detached = True
            log.warning("sync_cache_wait_timeout", cache=self.name, key=str(key),
                        timeout_s=self.wait_timeout_seconds)
            return build()
        if flight.failed:
            return build()
        return flight.value

    def _put(self, key: Hashable, value: Any) -> None:
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
