"""Serving a cached response over HTTP: conditional requests, headers, and the
rule that only successes are cached.

`core.cache.ResponseCache` stores rendered bytes and knows nothing about HTTP.
This is the other half — the ETag/304/Cache-Control dance every cached endpoint
would otherwise reimplement — kept in one place because the second caller
(the league-scored rankings route) arrived in the same change as the first.
"""

from __future__ import annotations

from typing import Awaitable, Callable

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
) -> Response:
    """Answer from `cache`, rendering once on a miss.

    `private=True` for anything resolved from the caller's identity — a shared
    cache must never be told it may hold it. A non-success envelope raised as
    `Uncacheable` comes back as its proper error response, uncached.
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
    headers = {
        "Cache-Control": f"{'private' if private else 'public'}, max-age={fresh_for}",
        "ETag": entry.etag,
        "X-Cache": "HIT" if hit else "MISS",
    }
    if etag_matches(request.headers.get("if-none-match"), entry.etag):
        return Response(status_code=304, headers=headers)
    return Response(content=entry.body, media_type="application/json", headers=headers)
