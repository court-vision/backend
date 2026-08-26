"""
Event-loop watchdog.

Peewee is synchronous, so a DB call that never returns (a hung DNS lookup, a
half-open socket) blocks the single uvicorn event loop and every request —
including `/ping` — hangs with nothing logged; Railway only restarts a
container that exits. This daemon thread posts a heartbeat onto the loop
every `interval` seconds and, when the loop has not run one for
`stall_seconds`, logs `event_loop_stuck`, flushes Sentry, and exits the
process so the platform's restart policy brings a fresh one up.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable, Optional

from core.logging import get_logger

EXIT_CODE = 70  # EX_SOFTWARE


def _default_on_stall(stalled_s: float) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_message(f"event loop stuck for {stalled_s:.0f}s; exiting", level="fatal")
        sentry_sdk.flush(timeout=2.0)
    except Exception:
        pass
    os._exit(EXIT_CODE)


class LoopWatchdog:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        stall_seconds: float,
        *,
        interval: float = 5.0,
        on_stall: Optional[Callable[[float], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._stall = stall_seconds
        self._interval = interval
        self._on_stall = on_stall or _default_on_stall
        self._clock = clock
        self._last_beat = clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)
        self.tripped = False

    def start(self) -> "LoopWatchdog":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval * 2)

    # Runs on the event loop; the only thing it does is prove the loop is alive.
    def _beat(self) -> None:
        self._last_beat = self._clock()

    def _run(self) -> None:
        log = get_logger("watchdog")
        while not self._stop.wait(self._interval):
            try:
                self._loop.call_soon_threadsafe(self._beat)
            except RuntimeError:  # loop closed: shutting down
                return
            stalled = self._clock() - self._last_beat
            if stalled >= self._stall and not self.tripped:
                self.tripped = True
                log.error("event_loop_stuck", stalled_s=round(stalled, 1), stall_threshold_s=self._stall)
                self._on_stall(stalled)
                return


def start_loop_watchdog(loop: asyncio.AbstractEventLoop, stall_seconds: float) -> Optional[LoopWatchdog]:
    """Start the watchdog unless disabled (`stall_seconds <= 0`)."""
    if stall_seconds <= 0:
        return None
    wd = LoopWatchdog(loop, stall_seconds).start()
    get_logger("watchdog").info("loop_watchdog_started", stall_threshold_s=stall_seconds)
    return wd
