"""
Writes each request's timing to ops.request_timings.

core/tracing.py's rollup puts every finished request on `queue`;
`keep_writing()` (started by main.py's lifespan) turns them into rows
(core/request_timing.summarize) and inserts them in one batch every
REQUEST_TIMINGS_FLUSH_SECONDS, and once an hour deletes rows older than
REQUEST_TIMINGS_RETENTION_DAYS. None of it runs on a request: a slow or failed
write costs the dashboard rows, never a response, and the queue drops new
requests rather than grow while the database is away.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from core import tracing
from core.logging import get_logger
from core.request_timing import RequestQueue, summarize
from core.settings import settings
from db.base import db_operation
from db.models.request_timing import RequestTiming

SERVICE = "api"
# Rows per insert; a busy minute is a few hundred requests.
BATCH = 500
PRUNE_EVERY_S = 3600

log = get_logger("request_timings")

queue = RequestQueue(maxlen=settings.request_timings_queue_max)


@db_operation("request_timings.insert")
def _write(requests: list) -> int:
    """Summarize and insert, on a DB worker: the summaries are CPU work, kept off the loop."""
    rows = []
    for server, spans in requests:
        try:
            row = summarize(server, spans, service=SERVICE, version=settings.version,
                            keep_trace_id=tracing.exporting())
        except Exception:
            log.exception("request_timing_summary_failed", span=server.name)
            continue
        if row is not None:
            rows.append(row)
    if rows:
        RequestTiming.insert_many(rows).execute()
    return len(rows)


@db_operation("request_timings.prune")
def _prune(cutoff: datetime) -> int:
    return RequestTiming.delete().where(RequestTiming.started_at < cutoff).execute()


async def flush() -> int:
    """Write everything queued now, a batch at a time. Returns the rows written."""
    written = 0
    while True:
        requests = queue.take(BATCH)
        if not requests:
            break
        written += await _write(requests)
        if len(requests) < BATCH:
            break
    dropped = queue.take_dropped()
    if dropped:
        log.warning("request_timings_dropped", dropped=dropped, queue_max=settings.request_timings_queue_max)
    return written


async def prune() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.request_timings_retention_days)
    return await _prune(cutoff)


async def keep_writing() -> None:
    """Flush on an interval and prune hourly, until cancelled. A failure is
    logged and the next interval tries again; a write that failed loses its
    batch (the rows are a sample of traffic, not a ledger)."""
    next_prune = time.monotonic()
    while True:
        await asyncio.sleep(settings.request_timings_flush_seconds)
        try:
            await flush()
        except Exception as exc:
            log.warning("request_timings_write_failed", error=type(exc).__name__)
        if time.monotonic() >= next_prune:
            next_prune = time.monotonic() + PRUNE_EVERY_S
            try:
                deleted = await prune()
                if deleted:
                    log.info("request_timings_pruned", deleted=deleted,
                             retention_days=settings.request_timings_retention_days)
            except Exception as exc:
                log.warning("request_timings_prune_failed", error=type(exc).__name__)
