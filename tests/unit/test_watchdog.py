"""Event-loop watchdog: trips only when the loop stops servicing heartbeats."""

import asyncio
import threading
import time

import pytest

from core.watchdog import LoopWatchdog, start_loop_watchdog


def _run_loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


@pytest.mark.unit
def test_healthy_loop_never_trips():
    loop, t = _run_loop_in_thread()
    stalls = []
    wd = LoopWatchdog(loop, stall_seconds=0.3, interval=0.05, on_stall=stalls.append).start()
    time.sleep(0.6)
    wd.stop()
    loop.call_soon_threadsafe(loop.stop); t.join(1)
    assert stalls == [] and wd.tripped is False


@pytest.mark.unit
def test_blocked_loop_trips_once_with_the_stall_duration():
    loop, t = _run_loop_in_thread()
    stalls = []
    wd = LoopWatchdog(loop, stall_seconds=0.3, interval=0.05, on_stall=stalls.append).start()
    loop.call_soon_threadsafe(time.sleep, 0.8)      # a synchronous call hogging the loop
    time.sleep(1.2)
    wd.stop()
    loop.call_soon_threadsafe(loop.stop); t.join(1)
    assert len(stalls) == 1 and stalls[0] >= 0.3 and wd.tripped is True


@pytest.mark.unit
def test_disabled_when_threshold_is_zero():
    loop = asyncio.new_event_loop()
    assert start_loop_watchdog(loop, 0) is None
    loop.close()
