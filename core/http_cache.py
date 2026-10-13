"""Serving a cached response over HTTP: conditional requests, headers, and the
rule that only successes are cached.

`core.cache.ResponseCache` stores rendered bytes and knows nothing about HTTP.
This is the other half — the ETag/304/Cache-Control dance every cached endpoint
would otherwise reimplement — kept in one place because the second caller
(the league-scored rankings route) arrived in the same change as the first.
`serve_cached_envelope` is the shape the public read routes share: a service
envelope, cached when it is a success, answered exactly as before when the
cache is switched off.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Union

from fastapi import Request, Response
from pydantic import BaseModel

from core.cache import CacheKey, ResponseCache
from core.compute import run_cpu
from core.responses import respond
from schemas.common import ApiStatus, BaseResponse


class Uncacheable(Exception):
    """Carries a non-success envelope back out of a cache's render step.

    Only successes are stored. Raising is how a render tells the cache to keep
    nothing; the envelope still reaches the caller and any coalesced follower.
    """

    def __init__(self, resp: BaseResponse):
        super().__init__(resp.message)
        self.resp = resp


def serialize_model(model: BaseModel) -> bytes:
    """The bytes FastAPI's `response_model` would have produced for this model."""
    return model.model_dump_json(by_alias=True).encode("utf-8")


async def render_envelope(operation: str, resp: BaseResponse) -> bytes:
    """Serialize a SUCCESS envelope off the event loop; refuse to cache anything else."""
    if resp.status != ApiStatus.SUCCESS:
        raise Uncacheable(resp)
    return await run_cpu(operation, serialize_model, resp)


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: `*`, or the tag among a comma-separated list."""
    if not if_none_match:
        return False
    candidates = [c.strip() for c in if_none_match.split(",")]
    if "*" in candidates:
        return True
    return any(c.removeprefix("W/") == etag for c in candidates)


async def serve_cached(
    request: Request,
    cache: ResponseCache,
    key: CacheKey,
    render: Callable[[], Awaitable[bytes]],
    *,
    private: bool = False,
    max_age: Optional[int] = None,
) -> Response:
    """Answer from `cache`, rendering once on a miss.

    `private=True` for anything resolved from the caller's identity — a shared
    cache must never be told it may hold it. A non-success envelope raised as
    `Uncacheable` comes back as its proper error response, uncached.
    `max_age` caps how long a client may hold its copy below the entry's own
    remaining life — for a body that goes stale at a known moment (a day
    rollover) sooner than the TTL would say.
    """
    entry = cache.get(key)
    hit = entry is not None
    if entry is None:
        try:
            entry = await cache.get_or_render(key, render)
        except Uncacheable as exc:
            return respond(exc.resp)

    # A client is never told to hold a copy longer than this replica's own entry.
    fresh_for = max(1, int(cache.ttl_seconds - entry.age_seconds()))
    if max_age is not None:
        fresh_for = max(1, min(fresh_for, int(max_age)))
    headers = {
        "Cache-Control": f"{'private' if private else 'public'}, max-age={fresh_for}",
        "ETag": entry.etag,
        "X-Cache": "HIT" if hit else "MISS",
    }
    if etag_matches(request.headers.get("if-none-match"), entry.etag):
        return Response(status_code=304, headers=headers)
    return Response(content=entry.body, media_type="application/json", headers=headers)


async def serve_cached_envelope(
    request: Request,
    cache: ResponseCache,
    key: CacheKey,
    produce: Callable[[], Awaitable[BaseResponse]],
    *,
    max_age: Optional[int] = None,
) -> Union[Response, BaseResponse]:
    """A public read route's whole body: the service's envelope, cached on success.

    With the cache switched off this is exactly the uncached route —
    `respond(await produce())`, rendered by FastAPI's `response_model`.

    The envelope is serialised on the event loop rather than through `run_cpu`
    as rankings does. These bodies are 0.2-17 KB, tens of microseconds in
    pydantic-core, the same work the `response_model` pass already did on the
    loop before they were cached; the CPU pool is two workers shared with
    category z-scoring, and queueing a player profile behind a Week-open burst
    would add latency and a new way for it to be shed with a 503.
    """
    if not cache.enabled:
        return respond(await produce())

    async def render() -> bytes:
        resp = await produce()
        if resp.status != ApiStatus.SUCCESS:
            raise Uncacheable(resp)
        return serialize_model(resp)

    return await serve_cached(request, cache, key, render, max_age=max_age)
