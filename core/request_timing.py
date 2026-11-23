"""
Where each request's time went.

Every API request runs inside one OpenTelemetry server span (core/tracing.py),
and each place it waits on something other than our own Python carries a child
span naming its bucket in `cv.bucket`: an ESPN, Yahoo or NBA call
(services/providers), the Claude API (services/ai), one of our internal
services (features, sqlmate, fantasy-writer), a Postgres query, a Redis
command, Clerk, or a queue for a free DB, CPU or provider slot ("wait").

`RequestRollup` holds each request's spans until its server span ends, then
hands the request to a sink (`RequestQueue`, drained by
services/request_timings.py). `summarize` turns one request into its
ops.request_timings row: the total, and the total split into buckets.

Splitting the total: calls overlap (one route reads the lineup and the free
agents at once), so each moment of the request is shared equally among the
buckets busy at that moment, and a moment with nothing outstanding is our own
code ("app"). The parts therefore always add up to the total. When spans nest
(the wait for a provider slot inside that provider's call), the innermost busy
span decides the bucket.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import SpanKind

# Span attribute naming a span's bucket.
BUCKET_ATTR = "cv.bucket"
# Set on the server span core/tracing.py opens for each request.
REQUEST_ATTR = "cv.request"

APP = "app"
OTHER_HTTP = "other_http"
BUCKETS = (
    "espn", "yahoo", "nba", "claude", "clerk", OTHER_HTTP,
    "features", "sqlmate", "fantasy_writer",
    "postgres", "redis", "wait", APP,
)

# A request with more spans than this (thousands of queries) keeps its first
# ones; the rest of its time reads as our own code.
MAX_SPANS_PER_REQUEST = 2000

RequestSink = Callable[[ReadableSpan, list[ReadableSpan]], None]


class RequestRollup(SpanProcessor):
    """Collects each request's spans in this process until the request ends.

    A span belongs to the request whose server span it descends from; spans
    outside any request (startup, background loops) are never tracked. Runs on
    whatever thread starts or ends a span (DB queries end on the DB workers),
    hence the lock; the work under it is a few dict operations.
    """

    def __init__(self, sink: RequestSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._request_of: dict[int, int] = {}  # span id -> its request's server span id
        self._open: dict[int, list[ReadableSpan]] = {}  # server span id -> its ended spans

    def on_start(self, span: Span, parent_context=None) -> None:
        span_id = span.get_span_context().span_id
        if span.kind is SpanKind.SERVER and span.attributes.get(REQUEST_ATTR):
            with self._lock:
                self._open[span_id] = []
                self._request_of[span_id] = span_id
            return
        parent = span.parent
        if parent is None:
            return
        with self._lock:
            request = self._request_of.get(parent.span_id)
            if request is not None and request in self._open:
                self._request_of[span_id] = request

    def on_end(self, span: ReadableSpan) -> None:
        span_id = span.get_span_context().span_id
        with self._lock:
            request = self._request_of.pop(span_id, None)
            if request is None:
                return
            if request != span_id:
                spans = self._open.get(request)
                # None: the request already ended (a task it started outlived it).
                if spans is not None and len(spans) < MAX_SPANS_PER_REQUEST:
                    spans.append(span)
                return
            spans = self._open.pop(span_id, [])
        self._sink(span, spans)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class RequestQueue:
    """Finished requests waiting to be written, oldest first.

    Bounded: when the writer falls behind, new requests are dropped and
    counted, so a database outage costs dashboard rows, never memory.
    """

    def __init__(self, maxlen: int) -> None:
        self._items: deque[tuple[ReadableSpan, list[ReadableSpan]]] = deque()
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self.dropped = 0

    def __call__(self, server: ReadableSpan, spans: list[ReadableSpan]) -> None:
        with self._lock:
            if len(self._items) >= self._maxlen:
                self.dropped += 1
                return
            self._items.append((server, spans))

    def __len__(self) -> int:
        return len(self._items)

    def take(self, limit: int) -> list[tuple[ReadableSpan, list[ReadableSpan]]]:
        with self._lock:
            count = min(limit, len(self._items))
            return [self._items.popleft() for _ in range(count)]

    def take_dropped(self) -> int:
        with self._lock:
            dropped, self.dropped = self.dropped, 0
            return dropped


def summarize(
    server: ReadableSpan,
    spans: Sequence[ReadableSpan],
    *,
    service: str,
    version: Optional[str],
    keep_trace_id: bool,
) -> Optional[dict]:
    """The ops.request_timings row for one request, or None for a request no
    route matched (scanners' 404s would only bury the real routes)."""
    attributes = server.attributes or {}
    route = attributes.get("http.route")
    if not route or server.start_time is None or server.end_time is None:
        return None
    start, end = server.start_time, max(server.end_time, server.start_time)

    spans = [s for s in spans if s.start_time is not None and s.end_time is not None]
    index = {s.get_span_context().span_id: i for i, s in enumerate(spans)}
    parent_of = [
        index.get(s.parent.span_id) if s.parent is not None else None
        for s in spans
    ]
    ancestors = [_ancestors(i, parent_of) for i in range(len(spans))]
    buckets = [_bucket(i, spans, parent_of) for i in range(len(spans))]

    parts = _split(start, end, spans, ancestors, buckets)
    calls: dict[str, int] = {}
    for i, span in enumerate(spans):
        # A call is the outermost client span of its bucket: a provider call's
        # retries, or the HTTP request inside a traced client call, are one call.
        if span.kind is not SpanKind.CLIENT:
            continue
        if any(spans[j].kind is SpanKind.CLIENT and buckets[j] == buckets[i] for j in ancestors[i]):
            continue
        calls[buckets[i]] = calls.get(buckets[i], 0) + 1

    context = server.get_span_context()
    return {
        "started_at": datetime.fromtimestamp(start / 1e9, tz=timezone.utc),
        "service": service,
        "method": attributes.get("http.request.method") or "GET",
        "route": route,
        "status_code": int(attributes.get("http.response.status_code") or 500),
        "duration_ms": round((end - start) / 1e6, 1),
        "parts": {bucket: round(ns / 1e6, 1) for bucket, ns in parts.items() if ns > 0},
        "calls": calls,
        "trace_id": f"{context.trace_id:032x}" if keep_trace_id and context.trace_flags.sampled else None,
        "version": version,
    }


def _ancestors(i: int, parent_of: list[Optional[int]]) -> frozenset[int]:
    seen: list[int] = []
    j = parent_of[i]
    while j is not None and j not in seen:
        seen.append(j)
        j = parent_of[j]
    return frozenset(seen)


def _bucket(i: int, spans: Sequence[ReadableSpan], parent_of: list[Optional[int]]) -> str:
    """The span's own bucket, else the nearest enclosing span's."""
    j: Optional[int] = i
    steps = 0
    while j is not None and steps <= len(spans):
        bucket = (spans[j].attributes or {}).get(BUCKET_ATTR)
        if bucket:
            return str(bucket)
        j = parent_of[j]
        steps += 1
    return OTHER_HTTP if spans[i].kind is SpanKind.CLIENT else APP


def _split(
    start: int,
    end: int,
    spans: Sequence[ReadableSpan],
    ancestors: list[frozenset[int]],
    buckets: list[str],
) -> dict[str, float]:
    """Nanoseconds per bucket over [start, end]; see the module docstring."""
    events: list[tuple[int, int, int]] = []
    for i, span in enumerate(spans):
        lo, hi = max(span.start_time, start), min(span.end_time, end)
        if hi > lo:
            events.append((lo, 1, i))
            events.append((hi, 0, i))
    events.sort()  # at one instant, ends (0) before starts (1)

    parts: dict[str, float] = {}
    active: set[int] = set()
    cursor = start
    for at, starts, i in events:
        if at > cursor:
            _share(parts, at - cursor, active, ancestors, buckets)
            cursor = at
        if starts:
            active.add(i)
        else:
            active.discard(i)
    if end > cursor:
        _share(parts, end - cursor, active, ancestors, buckets)
    return parts


def _share(
    parts: dict[str, float],
    width: int,
    active: set[int],
    ancestors: list[frozenset[int]],
    buckets: list[str],
) -> None:
    innermost = {
        buckets[i] for i in active
        if not any(i in ancestors[j] for j in active if j != i)
    }
    if not innermost:
        parts[APP] = parts.get(APP, 0.0) + width
        return
    each = width / len(innermost)
    for bucket in innermost:
        parts[bucket] = parts.get(bucket, 0.0) + each
