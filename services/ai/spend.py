"""
What the agent costs, and the monthly caps it runs under (docs/AI_AGENT_PLAN.md § 6).

Request counts don't fit an agent: one turn can be a single cached call or eight
calls over a long history. So turns are priced in dollars from their token
meters and summed per calendar month (UTC):

- the whole allowlist shares `ai_monthly_budget_usd`;
- each allowed user may spend that budget divided by the allowlist's size.

The sums read the ledger, usr.ai_spend, never the turns: deleting a
conversation deletes its turns, and must not hand their spend back. Before a
turn starts it reserves one turn's ceiling (`ai_agent_turn_max_usd`) there,
under a lock, so turns started at the same moment queue for the check instead
of all passing it; the reservation is refused while less than the ceiling is
left under either cap. Writing the turn settles the reservation to its real
cost. One that is never settled (the process died) stays charged at the
ceiling: the caps err high, never low. The Anthropic Console workspace spend
limit stays the last line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from peewee import fn

from core.errors import AppError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from db.base import db, db_operation
from db.models.ai_conversations import AiSpend
from schemas.common import ApiStatus

# Serializes reservations; any key no other code uses
_LEDGER_LOCK = 0x41_49_53_50


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


def _sums(user_id: int, since: datetime) -> tuple[int, int]:
    def total(query) -> int:
        return int(query.scalar() or 0)
    mine = total(AiSpend.select(fn.SUM(AiSpend.cost_micros))
                 .where(AiSpend.user_id == user_id, AiSpend.created_at >= since))
    everyone = total(AiSpend.select(fn.SUM(AiSpend.cost_micros)).where(AiSpend.created_at >= since))
    return mine, everyone


@db_operation("ai.month_spend")
def _month_spend(user_id: int, since: datetime) -> tuple[int, int]:
    return _sums(user_id, since)


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


def _caps() -> tuple[int, int]:
    total_cap = _micros(settings.ai_monthly_budget_usd)
    return total_cap // max(len(settings.ai_agent_user_ids), 1), total_cap


async def month_spend(user_id: int) -> Spend:
    mine, everyone = await _month_spend(user_id, month_start())
    user_cap, total_cap = _caps()
    return Spend(user=mine, total=everyone, user_cap=user_cap, total_cap=total_cap)


@db_operation("ai.reserve_turn")
def _reserve(user_id: int, since: datetime, amount: int, user_cap: int, total_cap: int) -> int | str:
    """The reservation's id, or which cap refused it ("total", "user"). The check
    and the insert happen under one transaction-scoped lock, so two turns can't
    both read the same total and both pass."""
    with db.atomic():
        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (_LEDGER_LOCK,))
        mine, everyone = _sums(user_id, since)
        if total_cap - everyone < amount:
            return "total"
        if user_cap - mine < amount:
            return "user"
        return AiSpend.insert(user_id=user_id, cost_micros=amount).execute()


async def reserve(user_id: int) -> int:
    """Reserve one turn's ceiling against both caps; returns the reservation's id."""
    user_cap, total_cap = _caps()
    outcome = await _reserve(user_id, month_start(), _micros(settings.ai_agent_turn_max_usd), user_cap, total_cap)
    if outcome == "total":
        raise ServiceUnavailableError(
            "AI_MONTHLY_BUDGET_REACHED",
            "The assistant has used this month's budget; it resets on the 1st",
            log_level="warning",
        )
    if outcome == "user":
        raise AppError(
            "AI_USER_BUDGET_REACHED",
            "You've used this month's assistant budget; it resets on the 1st",
            status_code=429,
            api_status=ApiStatus.RATE_LIMITED,
            log_level="info",
        )
    return outcome


@db_operation("ai.cancel_reservation")
def cancel(reservation_id: int) -> None:
    """Drop a reservation whose turn never started (nothing was spent)."""
    AiSpend.delete().where(AiSpend.id == reservation_id, AiSpend.settled == False).execute()  # noqa: E712


def settle_sync(reservation_id: int, cost: int, turn_id: int | None) -> None:
    """Set a reservation to its turn's real cost. Callers hold the transaction."""
    (AiSpend.update(cost_micros=cost, settled=True, turn_id=turn_id)
     .where(AiSpend.id == reservation_id)
     .execute())


@db_operation("ai.settle_reservation")
def settle(reservation_id: int, cost: int, turn_id: int | None = None) -> None:
    settle_sync(reservation_id, cost, turn_id)
