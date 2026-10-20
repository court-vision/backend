"""
OpenTelemetry for the api: one server span per request, a child span wherever
the request waits on something, and where those spans go.

`init_tracing(sink)` (main.py, at import) installs the tracer provider with
two destinations:

- always, `RequestRollup` (core/request_timing.py), which collects each
  request's spans and passes the finished request to `sink`, from which
  services/request_timings.py writes one row per request to
  ops.request_timings for the data-platform dashboard's Latency page;
- when Railway tracing is on for the service (Railway then sets
  OTEL_EXPORTER_OTLP_ENDPOINT on the next deploy), a batch exporter to
  Railway's collector, for each traced request's waterfall on its Traces page.

The spans are explicit, at the places a request leaves our own Python:
`timed()` around a provider, Claude, Clerk or internal-service call,
`waiting()` around a queue for a slot, and every Peewee query and Redis
command (wrapped here). There is no library auto-instrumentation: it would add
`traceparent` headers to every request to ESPN, Yahoo and the NBA, which have
no use for them (and the NBA CDN is picky about headers). Calls to our own
services carry one (`trace_headers`), so their spans join the request's trace
once those services are traced too.

Sampling: Railway's edge decides which requests it traces and says so in the
`traceparent` header it forwards. Export follows that decision, but every
request is recorded either way, because the rollup needs all of them. Spans
outside a request (startup, background loops) are not recorded at all.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import SpanKind
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.correlation_middleware import QUIET_PATHS, route_template
from core.request_timing import BUCKET_ATTR, REQUEST_ATTR, RequestRollup, RequestSink

tracer = trace.get_tracer("court-vision.api")

_installed = False
_exporting = False

_STATEMENT_MAX = 2000


def timed(bucket: str, name: str, *, call: bool = True, **attributes: Any):
    """A span for one call outside our own Python, counted in `bucket`.

    `with timed("espn", "GET espn"):` around the await. `call=False` is time
    in the bucket that is not a call of its own (a DB operation, whose queries
    are the calls). Exceptions mark the span as failed but are not recorded on
    it: their text can quote a provider's response.
    """
    return tracer.start_as_current_span(
        name,
        kind=SpanKind.CLIENT if call else SpanKind.INTERNAL,
        attributes={BUCKET_ATTR: bucket, **attributes},
        record_exception=False,
    )


def waiting(name: str):
    """A span for time spent queued for a slot (DB, CPU, provider, service)."""
    return tracer.start_as_current_span(
        name,
        kind=SpanKind.INTERNAL,
        attributes={BUCKET_ATTR: "wait"},
        record_exception=False,
    )


def trace_headers(headers: Optional[dict] = None) -> dict:
    """`headers` plus the current trace's `traceparent`, for calls to our own services."""
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def exporting() -> bool:
    """Whether spans go to Railway, i.e. whether a stored trace id can be opened there."""
    return _exporting


class _RequestSampler(Sampler):
    """Record every request; export the ones Railway's edge traces.

    No parent: a server span is a request that came without a `traceparent`
    (Railway tracing off, or a caller inside the private network), sampled;
    any other span outside a request is dropped. A remote parent is the
    edge's decision, followed for export but always recorded. Below that,
    children follow their parent.
    """

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                      links=None, trace_state=None) -> SamplingResult:
        parent = trace.get_current_span(parent_context)
        context = parent.get_span_context()
        if not context.is_valid:
            if kind is SpanKind.SERVER:
                return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
            return SamplingResult(Decision.DROP)
        if not context.is_remote and not parent.is_recording():
            return SamplingResult(Decision.DROP, trace_state=context.trace_state)
        decision = Decision.RECORD_AND_SAMPLE if context.trace_flags.sampled else Decision.RECORD_ONLY
        return SamplingResult(decision, attributes, context.trace_state)

    def get_description(self) -> str:
        return "CourtVisionRequestSampler"


def init_tracing(sink: RequestSink, *, service_name: str, version: str, database: Any = None) -> bool:
    """Install the tracer provider (once per process). Returns True when it did."""
    global _installed, _exporting
    if _installed:
        return False

    attributes = {"service.version": os.environ.get("OTEL_SERVICE_VERSION") or version}
    if not os.environ.get("OTEL_SERVICE_NAME"):
        attributes["service.name"] = service_name
    provider = TracerProvider(resource=Resource.create(attributes), sampler=_RequestSampler())
    provider.add_span_processor(RequestRollup(sink))

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _exporting = True

    trace.set_tracer_provider(provider)
    if database is not None:
        _trace_queries(database)
    _trace_redis()
    _installed = True
    return True


def _trace_queries(database: Any) -> None:
    """One span per Peewee query (every query goes through `execute_sql`)."""
    execute_sql = database.execute_sql

    def traced_execute_sql(sql, *args, **kwargs):
        statement = sql if isinstance(sql, str) else str(sql)
        with tracer.start_as_current_span(
            (statement.split(None, 1) or ["query"])[0].upper(),
            kind=SpanKind.CLIENT,
            attributes={
                BUCKET_ATTR: "postgres",
                "db.system.name": "postgresql",
                # The parameterised text only: values travel separately and are never recorded.
                "db.query.text": statement[:_STATEMENT_MAX],
            },
            record_exception=False,
        ):
            return execute_sql(sql, *args, **kwargs)

    database.execute_sql = traced_execute_sql


def _trace_redis() -> None:
    """One span per Redis command. Redis holds the rate-limit counters, read on
    every rate-limited request (core/rate_limit.py)."""
    try:
        import redis
    except ImportError:  # pragma: no cover - redis is a pinned dependency
        return
    execute_command = redis.Redis.execute_command

    def traced_execute_command(self, *args, **options):
        command = str(args[0]).upper() if args else "COMMAND"
        with tracer.start_as_current_span(
            command,
            kind=SpanKind.CLIENT,
            attributes={BUCKET_ATTR: "redis", "db.system.name": "redis"},
            record_exception=False,
        ):
            return execute_command(self, *args, **options)

    redis.Redis.execute_command = traced_execute_command


class RequestSpanMiddleware:
    """Opens each request's server span; the outermost of the app's middlewares.

    Continues the trace Railway's edge started when the request carries a
    `traceparent`. The route template (`/v1/players/{player_id}/stats`) and the
    status are set when the response is done. Liveness and health polls and
    CORS preflights get no span.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope.get("method") == "OPTIONS"
                or scope.get("path") in QUIET_PATHS):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        method = scope.get("method") or "GET"
        status: Optional[int] = None

        async def send_noting_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with tracer.start_as_current_span(
            method,
            context=propagate.extract(headers),
            kind=SpanKind.SERVER,
            attributes={REQUEST_ATTR: True, "http.request.method": method, "url.path": scope.get("path") or ""},
            record_exception=False,
        ) as span:
            try:
                await self.app(scope, receive, send_noting_status)
            except asyncio.CancelledError:
                status = status or 499  # the client went away
                raise
            finally:
                route = route_template(Request(scope))
                if route:
                    span.set_attribute("http.route", route)
                    span.update_name(f"{method} {route}")
                span.set_attribute("http.response.status_code", status or 500)
