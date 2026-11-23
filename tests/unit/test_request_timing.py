"""
core/request_timing.py: how one request's spans become its ops.request_timings
row, and how the rollup collects a request's spans.

`summarize` is checked on hand-built spans (times in ms from the request's
start) so each split can be worked out on paper; the rollup and the sampler
run on a real tracer provider of their own, never the process-wide one.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

from core.request_timing import (
    BUCKET_ATTR,
    MAX_SPANS_PER_REQUEST,
    REQUEST_ATTR,
    RequestQueue,
    RequestRollup,
    summarize,
)
from core.tracing import _RequestSampler
from db.models.request_timing import RequestTiming

TRACE_ID = 0x0123456789ABCDEF0123456789ABCDEF
T0 = 1_760_000_000_000_000_000  # ns
MS = 1_000_000


def _context(span_id: int, *, sampled: bool = True) -> SpanContext:
    flags = TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT)
    return SpanContext(TRACE_ID, span_id, is_remote=False, trace_flags=flags)


def _server(start: float = 0, end: float = 100, *, route="/v1/things/{thing_id}", status=200,
            sampled=True) -> ReadableSpan:
    attributes = {REQUEST_ATTR: True, "http.request.method": "GET", "http.response.status_code": status}
    if route:
        attributes["http.route"] = route
    return ReadableSpan(
        name="GET", context=_context(1, sampled=sampled), kind=SpanKind.SERVER, attributes=attributes,
        start_time=T0 + int(start * MS), end_time=T0 + int(end * MS),
    )


def _span(span_id: int, start: float, end: float, *, bucket=None, parent: int = 1,
          kind=SpanKind.CLIENT) -> ReadableSpan:
    return ReadableSpan(
        name=f"span {span_id}", context=_context(span_id), parent=_context(parent), kind=kind,
        attributes={BUCKET_ATTR: bucket} if bucket else {},
        start_time=T0 + int(start * MS), end_time=T0 + int(end * MS),
    )


def _row(server, spans, *, keep_trace_id=False):
    return summarize(server, spans, service="api", version="abc123", keep_trace_id=keep_trace_id)


@pytest.mark.unit
def test_sequential_calls_split_the_request_and_the_rest_is_our_code():
    row = _row(_server(), [_span(2, 10, 60, bucket="espn"), _span(3, 70, 80, bucket="postgres")])

    assert row["parts"] == {"espn": 50.0, "postgres": 10.0, "app": 40.0}
    assert row["calls"] == {"espn": 1, "postgres": 1}
    assert row["duration_ms"] == 100.0
    assert row["route"] == "/v1/things/{thing_id}"
    assert (row["service"], row["method"], row["status_code"], row["version"]) == ("api", "GET", 200, "abc123")
    assert row["started_at"].timestamp() == pytest.approx(T0 / 1e9)


@pytest.mark.unit
def test_overlapping_calls_share_the_time_they_overlap():
    row = _row(_server(), [_span(2, 10, 60, bucket="espn"), _span(3, 30, 80, bucket="yahoo")])

    # 10-30 ESPN alone, 30-60 both (15 each), 60-80 Yahoo alone
    assert row["parts"] == {"espn": 35.0, "yahoo": 35.0, "app": 30.0}
    assert sum(row["parts"].values()) == pytest.approx(row["duration_ms"])


@pytest.mark.unit
def test_the_innermost_span_decides_the_bucket():
    # The wait for a provider slot runs inside that provider's call.
    row = _row(_server(), [
        _span(2, 10, 90, bucket="espn"),
        _span(3, 10, 30, bucket="wait", parent=2, kind=SpanKind.INTERNAL),
    ])

    assert row["parts"] == {"wait": 20.0, "espn": 60.0, "app": 20.0}
    assert row["calls"] == {"espn": 1}


@pytest.mark.unit
def test_a_db_operation_is_postgres_time_and_its_queries_are_the_calls():
    row = _row(_server(), [
        _span(2, 10, 50, bucket="postgres", kind=SpanKind.INTERNAL),
        _span(3, 15, 20, bucket="postgres", parent=2),
        _span(4, 30, 40, bucket="postgres", parent=2),
    ])

    assert row["parts"] == {"postgres": 40.0, "app": 60.0}
    assert row["calls"] == {"postgres": 2}


@pytest.mark.unit
def test_an_unlabelled_span_takes_its_enclosing_bucket_and_is_not_a_second_call():
    row = _row(_server(), [
        _span(2, 10, 50, bucket="features"),
        _span(3, 12, 48, parent=2),  # e.g. the HTTP request inside the features call
        _span(4, 60, 70),            # an unlabelled outbound call of its own
        _span(5, 80, 90, kind=SpanKind.INTERNAL),
    ])

    assert row["parts"] == {"features": 40.0, "other_http": 10.0, "app": 50.0}
    assert row["calls"] == {"features": 1, "other_http": 1}


@pytest.mark.unit
def test_time_outside_the_request_is_clipped():
    row = _row(_server(0, 100), [_span(2, -20, 30, bucket="redis"), _span(3, 90, 150, bucket="espn")])

    assert row["parts"] == {"redis": 30.0, "espn": 10.0, "app": 60.0}
    assert row["duration_ms"] == 100.0


@pytest.mark.unit
def test_an_unmatched_request_has_no_row():
    assert _row(_server(route=None, status=404), []) is None


@pytest.mark.unit
def test_trace_id_is_kept_only_for_a_trace_railway_has():
    assert _row(_server(), [], keep_trace_id=True)["trace_id"] == f"{TRACE_ID:032x}"
    assert _row(_server(sampled=False), [], keep_trace_id=True)["trace_id"] is None
    assert _row(_server(), [], keep_trace_id=False)["trace_id"] is None


@pytest.mark.unit
def test_a_row_has_only_the_table_columns():
    row = _row(_server(), [_span(2, 10, 60, bucket="espn")])

    assert set(row) <= set(RequestTiming._meta.fields) - {"id"}


# ---- the rollup and the sampler, on a provider of their own ----

def _tracer(sink, rollup=None):
    provider = TracerProvider(sampler=_RequestSampler())
    provider.add_span_processor(rollup or RequestRollup(sink))
    return provider.get_tracer("test")


def _start_request(tracer, context=None):
    return tracer.start_as_current_span(
        "GET", context=context, kind=SpanKind.SERVER,
        attributes={REQUEST_ATTR: True, "http.request.method": "GET"},
    )


@pytest.mark.unit
def test_the_rollup_hands_over_each_request_with_its_own_spans():
    finished = []
    rollup = RequestRollup(lambda server, spans: finished.append((server, spans)))
    tracer = _tracer(None, rollup)

    with tracer.start_as_current_span("startup query", kind=SpanKind.CLIENT):
        pass  # outside any request: not recorded
    with _start_request(tracer) as server:
        with tracer.start_as_current_span("GET espn", kind=SpanKind.CLIENT, attributes={BUCKET_ATTR: "espn"}):
            with tracer.start_as_current_span("espn slot", attributes={BUCKET_ATTR: "wait"}):
                pass
        server.set_attribute("http.route", "/v1/things/{thing_id}")
    with _start_request(tracer):
        with tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT, attributes={BUCKET_ATTR: "postgres"}):
            pass

    assert [sorted(s.name for s in spans) for _, spans in finished] == [["GET espn", "espn slot"], ["SELECT"]]
    assert finished[0][0].attributes["http.route"] == "/v1/things/{thing_id}"
    assert rollup._open == {} and rollup._request_of == {}  # nothing held once requests end


@pytest.mark.unit
def test_a_span_that_outlives_its_request_is_left_out():
    finished = []
    tracer = _tracer(lambda server, spans: finished.append(spans))

    with _start_request(tracer):
        straggler = tracer.start_span("background", attributes={BUCKET_ATTR: "espn"})
    straggler.end()

    assert finished == [[]]


@pytest.mark.unit
def test_the_rollup_keeps_at_most_the_first_spans_of_a_huge_request():
    finished = []
    tracer = _tracer(lambda server, spans: finished.append(spans))

    with _start_request(tracer):
        for _ in range(MAX_SPANS_PER_REQUEST + 5):
            with tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT):
                pass

    assert len(finished[0]) == MAX_SPANS_PER_REQUEST


@pytest.mark.unit
def test_the_sampler_records_every_request_and_exports_what_the_edge_sampled():
    tracer = _tracer(lambda server, spans: None)

    def edge(sampled: bool):
        flags = TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT)
        remote = SpanContext(TRACE_ID, 0xABC, is_remote=True, trace_flags=flags)
        return trace.set_span_in_context(NonRecordingSpan(remote))

    with _start_request(tracer) as no_header:
        with tracer.start_as_current_span("child") as child:
            assert child.is_recording() and child.get_span_context().trace_flags.sampled
    assert no_header.get_span_context().trace_flags.sampled

    with _start_request(tracer, edge(sampled=True)) as traced:
        assert traced.is_recording() and traced.get_span_context().trace_flags.sampled
        assert traced.get_span_context().trace_id == TRACE_ID

    with _start_request(tracer, edge(sampled=False)) as untraced:
        with tracer.start_as_current_span("child") as child:
            assert child.is_recording() and not child.get_span_context().trace_flags.sampled
        assert untraced.is_recording() and not untraced.get_span_context().trace_flags.sampled

    with tracer.start_as_current_span("background loop") as orphan:
        assert not orphan.is_recording()
        with tracer.start_as_current_span("its query", kind=SpanKind.CLIENT) as query:
            assert not query.is_recording()


# ---- the queue ----

@pytest.mark.unit
def test_the_queue_drops_new_requests_when_full_and_counts_them():
    queue = RequestQueue(maxlen=2)
    for n in range(4):
        queue(_server(), [_span(n + 2, 0, 1)])

    assert len(queue) == 2
    assert queue.take_dropped() == 2 and queue.take_dropped() == 0
    assert len(queue.take(1)) == 1 and len(queue.take(10)) == 1 and queue.take(10) == []
