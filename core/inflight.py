"""Single-flight for identical in-flight reads, fenced by a process-wide write epoch.

One Week open fires about 11 ESPN reads at once. Three of them are the same full
league read (/matchups/week, /live and /current each value the same matchup)
and two are the same roster read (/lineup and /actions). Each took a slot of the
process-wide ESPN cap of 8, so the burst queued 130-220 ms for a slot
(docs/PERF_AUDIT_2026-10.md, item A6). `InFlight` lets concurrent identical
calls share one execution; the provider layer keys it on everything that makes
two reads the same read (services/providers/http.py).

Rules:

- **In flight only, never a cache.** A caller joins an execution that is still
  running. The entry leaves the map inside the execution itself, before its
  result is published, so a call made after another one finished always runs
  again.
- **Fenced by writes.** `provider_write()` brackets every provider write and
  bumps the epoch when the write starts and again when it ends, failures
  included. Callers put `write_epoch()` in the key, so a read joins only an
  execution that started in the same epoch: the verify re-read after a lineup
  move, and the board the client refetches after it, never receive a response
  that began before the write finished.
- **Shielded.** The shared execution runs in its own task. A caller that gives
  up (cancelled, timed out) leaves the others waiting on it; only when the last
  waiter has gone is the execution itself cancelled, which is what a lone
  caller's cancellation did before coalescing.
- **Per process.** Each replica coalesces its own reads, and the fence covers
  the writes made by this process. Today that is every write: the API is the
  fantasy-writer's only client and runs as one process on one replica.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Awaitable, Callable, Hashable, Iterator, Optional, TypeVar

from core.logging import get_logger

log = get_logger("inflight")

T = TypeVar("T")

_write_epoch = 0


def write_epoch() -> int:
    """The current write epoch; part of every coalescing key."""
    return _write_epoch


def _bump_write_epoch() -> None:
    global _write_epoch
    _write_epoch += 1


@contextmanager
def provider_write() -> Iterator[None]:
    """Bracket one provider write so no read can join across it.

    The bump at the start keeps reads that begin during the write from joining
    one that began before it; the bump at the end (in `finally`, so a failed or
    cancelled write counts too) keeps every read issued after the write
    returned from joining one that began while it was still running.
    """
    _bump_write_epoch()
    try:
        yield
    finally:
        _bump_write_epoch()


@dataclass(eq=False)
class _Flight:
    started_at: float = field(default_factory=time.perf_counter)
    task: Optional[asyncio.Task] = None
    waiters: int = 0
    joined: int = 0


class InFlight:
    """Concurrent `run` calls with an equal key share one execution of `call`.

    Every waiter receives the execution's own return value (callers that might
    mutate it must share something immutable, as the provider layer shares the
    httpx.Response and parses it per caller) or the same exception.
    """

    def __init__(self, name: str):
        self.name = name
        self._event = f"{name}_coalesced"
        self._flights: dict[Hashable, _Flight] = {}
        self.started = 0
        self.coalesced = 0

    async def run(
        self,
        key: Hashable,
        call: Callable[[], Awaitable[T]],
        *,
        log_fields: Optional[dict[str, Any]] = None,
    ) -> T:
        flight = self._flights.get(key)
        # A flight stranded on another event loop (a finished test's) can never
        # finish on this one; it is replaced, never joined.
        if (flight is not None and flight.task is not None and not flight.task.done()
                and flight.task.get_loop() is asyncio.get_running_loop()):
            flight.joined += 1
            self.coalesced += 1
            task = flight.task
        else:
            flight = _Flight()
            self._flights[key] = flight
            task = flight.task = asyncio.create_task(self._fly(key, flight, call, log_fields or {}))
            task.add_done_callback(partial(self._landed, key, flight))
            self.started += 1
        flight.waiters += 1
        try:
            # Shielded: this caller being cancelled must not cancel the shared call.
            return await asyncio.shield(task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not task.done():
                # Only a cancelled caller leaves before the call is done, and this was
                # the last one: nobody wants the result, so stop the call as a lone
                # caller's cancellation always did. It leaves the map first, so a
                # caller arriving now starts afresh instead of joining a cancellation.
                self._forget(key, flight)
                task.cancel()

    async def _fly(self, key: Hashable, flight: _Flight, call: Callable[[], Awaitable[T]],
                   log_fields: dict[str, Any]) -> T:
        try:
            return await call()
        finally:
            # Out of the map before the result is published: a caller arriving
            # after this point starts a new call rather than reading a finished one.
            self._forget(key, flight)
            if flight.joined:
                log.info(self._event, joined=flight.joined,
                         elapsed_ms=round((time.perf_counter() - flight.started_at) * 1000), **log_fields)

    def _landed(self, key: Hashable, flight: _Flight, task: asyncio.Task) -> None:
        # A task cancelled before it first ran never enters `_fly`'s finally.
        self._forget(key, flight)
        if not task.cancelled():
            # Mark a failure as retrieved even when every waiter has already gone,
            # so asyncio does not log it as never retrieved; waiters still see it.
            task.exception()

    def _forget(self, key: Hashable, flight: _Flight) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "in_flight": len(self._flights),
            "started": self.started,
            "coalesced": self.coalesced,
        }
