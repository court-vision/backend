"""
Checks that run before any model call: the kill switch, then the daily quotas.

Quotas count requests, not tokens, in fixed 24-hour windows that start at a
caller's first request. They live in the rate limiter's shared storage (Redis
on Railway), so every replica sees the same count. A request that later fails
upstream still counts: it may already have spent tokens.
"""

from __future__ import annotations

import asyncio

from limits import parse

from core.errors import AppError, ServiceUnavailableError
from core.rate_limit import quota_limiter
from core.settings import settings
from schemas.common import ApiStatus

_NAMESPACE = ("courtvision", "ai")


def ensure_enabled() -> None:
    if not settings.ai_enabled:
        raise ServiceUnavailableError("AI_DISABLED", "The assistant is turned off", log_level="info")


def _user_exhausted() -> AppError:
    return AppError(
        "AI_QUOTA_EXCEEDED",
        "You've used today's assistant questions; they reset within 24 hours",
        status_code=429,
        api_status=ApiStatus.RATE_LIMITED,
        log_level="info",
    )


def _global_exhausted() -> ServiceUnavailableError:
    return ServiceUnavailableError(
        "AI_DAILY_BUDGET_REACHED",
        "The assistant has reached today's limit; try again tomorrow",
        log_level="warning",
    )


async def consume_quota(user_id: int) -> None:
    """Spend one request from the caller's allowance and one from the global one.

    Both are checked before either is charged, because a request turned away by
    one budget must not spend the other. Charging as we go would mean a caller
    retrying through a spent global budget burns their own day's allowance on
    requests no model ever saw -- and, since the two windows start at different
    times, stays blocked after capacity returns; in the other direction, one
    caller past their own limit would drain the budget everyone shares.
    """
    user_item = parse(f"{settings.ai_user_daily_limit}/day")
    user_key = (*_NAMESPACE, f"user:{user_id}")
    global_item = parse(f"{settings.ai_global_daily_limit}/day")
    global_key = (*_NAMESPACE, "global")

    if not await asyncio.to_thread(quota_limiter.test, user_item, *user_key):
        raise _user_exhausted()
    if not await asyncio.to_thread(quota_limiter.test, global_item, *global_key):
        raise _global_exhausted()

    # Two concurrent requests can both pass the checks above and then both
    # charge, which costs at most one extra question a day -- cheaper than
    # holding a lock across the pair.
    if not await asyncio.to_thread(quota_limiter.hit, user_item, *user_key):
        raise _user_exhausted()
    if not await asyncio.to_thread(quota_limiter.hit, global_item, *global_key):
        raise _global_exhausted()
