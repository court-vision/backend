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


async def consume_quota(user_id: int) -> None:
    """Spend one request from the caller's allowance, then from the global one."""
    user_item = parse(f"{settings.ai_user_daily_limit}/day")
    if not await asyncio.to_thread(quota_limiter.hit, user_item, *_NAMESPACE, f"user:{user_id}"):
        raise AppError(
            "AI_QUOTA_EXCEEDED",
            "You've used today's assistant questions; they reset within 24 hours",
            status_code=429,
            api_status=ApiStatus.RATE_LIMITED,
            log_level="info",
        )
    global_item = parse(f"{settings.ai_global_daily_limit}/day")
    if not await asyncio.to_thread(quota_limiter.hit, global_item, *_NAMESPACE, "global"):
        raise ServiceUnavailableError(
            "AI_DAILY_BUDGET_REACHED",
            "The assistant has reached today's limit; try again tomorrow",
            log_level="warning",
        )
