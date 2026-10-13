"""
What the agent costs, and the monthly caps it runs under (docs/AI_AGENT_PLAN.md § 6).

Request counts don't fit an agent: one turn can be a single cached call or eight
calls over a long history. So turns are priced in dollars from their token
meters, stored on the turn (`usr.ai_turns.cost_micros`), and summed per
calendar month (UTC):

- the whole allowlist shares `ai_monthly_budget_usd`;
- each allowed user may spend that budget divided by the allowlist's size.

A turn is refused while less than one turn's ceiling (`ai_agent_turn_max_usd`)
is left under either cap, so no turn can carry spend past it by more than its
own overrun. The Anthropic Console workspace spend limit stays the last line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from peewee import fn

from core.errors import AppError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from db.base import db_operation
from db.models.ai_conversations import AiTurn
from schemas.common import ApiStatus


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens. `input` is uncached input only: the API
    reports cache reads and cache writes as separate meters."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# Published rates (5-minute cache writes at 1.25x input). A fallback model can
# serve a declined request, so the fallback targets are priced too.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-opus-5": Price(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Price(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
}
# An unknown model is priced at the dearest known rates: a cap that undercounts
# is worse than one that stops a little early.
_DEAREST = max(PRICES.values(), key=lambda p: p.output)


def price_of(model: str) -> Price:
    for name in sorted(PRICES, key=len, reverse=True):
        if model == name or model.startswith(f"{name}-"):
            return PRICES[name]
    get_logger().warning("ai_unpriced_model", model=model)
    return _DEAREST


def cost_micros(model: str, *, input_tokens: int, output_tokens: int,
                cache_read_input_tokens: int, cache_creation_input_tokens: int) -> int:
    """Millionths of a dollar. Every token is priced at the serving model's rates,
    including a declined attempt's: fallback targets cost more than Opus 5.5, so
    this errs high."""
    p = price_of(model)
    dollars_per_million = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_input_tokens * p.cache_read
        + cache_creation_input_tokens * p.cache_write
    )
    # tokens x $/MTok = micro-dollars, exactly
    return round(dollars_per_million)


def month_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@db_operation("ai.month_spend")
def _month_spend(user_id: int, since: datetime) -> tuple[int, int]:
    def total(query) -> int:
        return int(query.scalar() or 0)
    mine = total(AiTurn.select(fn.SUM(AiTurn.cost_micros))
                 .where(AiTurn.user_id == user_id, AiTurn.created_at >= since))
    everyone = total(AiTurn.select(fn.SUM(AiTurn.cost_micros)).where(AiTurn.created_at >= since))
    return mine, everyone


@dataclass(frozen=True)
class Spend:
    """This month's spend and caps, in micro-dollars."""

    user: int
    total: int
    user_cap: int
    total_cap: int

    @property
    def user_left(self) -> int:
        return max(self.user_cap - self.user, 0)

    @property
    def total_left(self) -> int:
        return max(self.total_cap - self.total, 0)


def _micros(dollars: float) -> int:
    return round(dollars * 1_000_000)


async def month_spend(user_id: int) -> Spend:
    mine, everyone = await _month_spend(user_id, month_start())
    total_cap = _micros(settings.ai_monthly_budget_usd)
    return Spend(
        user=mine,
        total=everyone,
        user_cap=total_cap // max(len(settings.ai_agent_user_ids), 1),
        total_cap=total_cap,
    )


async def ensure_budget(user_id: int) -> Spend:
    """Refuse a turn while either cap has less than one turn's ceiling left."""
    spend = await month_spend(user_id)
    reserve = _micros(settings.ai_agent_turn_max_usd)
    if spend.total_left < reserve:
        raise ServiceUnavailableError(
            "AI_MONTHLY_BUDGET_REACHED",
            "The assistant has used this month's budget; it resets on the 1st",
            log_level="warning",
        )
    if spend.user_left < reserve:
        raise AppError(
            "AI_USER_BUDGET_REACHED",
            "You've used this month's assistant budget; it resets on the 1st",
            status_code=429,
            api_status=ApiStatus.RATE_LIMITED,
            log_level="info",
        )
    return spend
