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

Dime's check-ins (services/ai/checkins.py) spend on a line of their own,
`autopilot`, with one monthly budget for every autopilot team
(`autopilot_monthly_budget_usd`): the caps above read the `desk` line only, so
a day of check-ins can't use up the AI desk, and the other way round.

The live rankings' analyst (services/ai/analyst.py) has a third line, `analyst`,
with its own monthly budget (`analyst_monthly_budget_usd`). Its reads belong to
no user, so their rows carry none; each reserves `analyst_read_max_usd` before
its one model call and is settled to what the call cost. A call that went out
and never answered (a timeout, a dropped connection) keeps its reservation:
the API may bill a request it accepted, whoever stopped listening.

Every cap can be changed without a deploy (migration 0054): a row in usr.ai_caps
replaces a line's number from the environment, or sets one user's cap on the desk
or autopilot line. With no row the environment applies, and a user's desk cap is
the desk's cap (as set) divided by the allowlist, as before; on the autopilot line
a user has no cap of their own until a row gives one. The data platform's AI spend
page writes them (services/ai/spend_report.py). A reservation reads its caps just
before it takes the ledger lock, so a change applies to every reservation that
starts after it commits; one already waiting on the lock (milliseconds) keeps the
caps it read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from peewee import fn

from core.errors import AppError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from db.base import db, db_operation
from db.models.ai_conversations import AiCap, AiSpend
from schemas.common import ApiStatus

# Serializes reservations; any key no other code uses
_LEDGER_LOCK = 0x41_49_53_50
DESK, AUTOPILOT, ANALYST = "desk", "autopilot", "analyst"
LINES = (DESK, AUTOPILOT, ANALYST)


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


def _total(query) -> int:
    return int(query.scalar() or 0)


def _sums(user_id: int, since: datetime) -> tuple[int, int]:
    """The AI desk's line: this user's and everyone's, since `since`."""
    desk = (AiSpend.created_at >= since) & (AiSpend.line == DESK)
    mine = _total(AiSpend.select(fn.SUM(AiSpend.cost_micros)).where(desk & (AiSpend.user_id == user_id)))
    everyone = _total(AiSpend.select(fn.SUM(AiSpend.cost_micros)).where(desk))
    return mine, everyone


def _line_sum(line: str, since: datetime) -> int:
    return _total(AiSpend.select(fn.SUM(AiSpend.cost_micros))
                  .where(AiSpend.created_at >= since, AiSpend.line == line))


def _autopilot_sum(since: datetime) -> int:
    return _line_sum(AUTOPILOT, since)


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


def _micros(dollars: float | Decimal) -> int:
    return round(dollars * 1_000_000)


def default_line_usd(line: str) -> float:
    """A line's monthly budget from the environment, which a cap row replaces."""
    return {
        DESK: settings.ai_monthly_budget_usd,
        AUTOPILOT: settings.autopilot_monthly_budget_usd,
        ANALYST: settings.analyst_monthly_budget_usd,
    }[line]


def desk_share(line_cap: int) -> int:
    """A user's desk cap when no row sets one: an equal part of the desk's cap."""
    return line_cap // max(len(settings.ai_agent_user_ids), 1)


@dataclass(frozen=True)
class Caps:
    """The monthly caps one line runs under, in micro-dollars. `user` is the given
    user's cap on the line, or None when they have none of their own."""

    line: int
    user: Optional[int] = None


def cap_rows_sync(line: str, user_id: Optional[int] = None) -> dict[Optional[int], Decimal]:
    """The line's cap row (key None) and the user's, as set from the dashboard."""
    who = AiCap.user_id.is_null()
    if user_id is not None:
        who = who | (AiCap.user_id == user_id)
    return {row.user_id_id: row.monthly_usd
            for row in AiCap.select(AiCap.user_id, AiCap.monthly_usd).where(AiCap.line == line, who)}


def caps_sync(line: str, user_id: Optional[int] = None) -> Caps:
    rows = cap_rows_sync(line, user_id)
    line_cap = _micros(rows[None]) if None in rows else _micros(default_line_usd(line))
    if user_id is None or line == ANALYST:
        return Caps(line=line_cap)
    if user_id in rows:
        return Caps(line=line_cap, user=_micros(rows[user_id]))
    return Caps(line=line_cap, user=desk_share(line_cap) if line == DESK else None)


@db_operation("ai.caps")
def _caps(line: str, user_id: Optional[int] = None) -> Caps:
    return caps_sync(line, user_id)


async def month_spend(user_id: int) -> Spend:
    mine, everyone = await _month_spend(user_id, month_start())
    caps = await _caps(DESK, user_id)
    return Spend(user=mine, total=everyone, user_cap=caps.user, total_cap=caps.line)


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
    caps = await _caps(DESK, user_id)
    outcome = await _reserve(user_id, month_start(), _micros(settings.ai_agent_turn_max_usd), caps.user, caps.line)
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


@db_operation("ai.reserve_checkin")
def _reserve_autopilot(user_id: int, since: datetime, amount: int, cap: int,
                       user_cap: Optional[int] = None) -> int | None:
    """A check-in's reservation on the autopilot line, or None when the line's budget,
    or the user's own cap on it (when one is set), can't take one more turn's ceiling.
    Under the same lock as the desk's reservations."""
    with db.atomic():
        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (_LEDGER_LOCK,))
        if cap - _autopilot_sum(since) < amount:
            return None
        if user_cap is not None:
            theirs = _total(AiSpend.select(fn.SUM(AiSpend.cost_micros)).where(
                AiSpend.created_at >= since, AiSpend.line == AUTOPILOT, AiSpend.user_id == user_id))
            if user_cap - theirs < amount:
                return None
        return AiSpend.insert(user_id=user_id, cost_micros=amount, line=AUTOPILOT).execute()


async def reserve_autopilot(user_id: int) -> int | None:
    """Reserve one check-in's ceiling on the autopilot line; None when this month's
    budget for check-ins, or the user's own cap on it, is used up (the check-in is
    skipped, not failed)."""
    caps = await _caps(AUTOPILOT, user_id)
    return await _reserve_autopilot(user_id, month_start(), _micros(settings.ai_agent_turn_max_usd),
                                    caps.line, caps.user)


@db_operation("ai.autopilot_month_spend")
def _autopilot_month(since: datetime) -> int:
    return _autopilot_sum(since)


async def autopilot_month_spend() -> tuple[int, int]:
    """This month's check-in spend and the line's cap, in micro-dollars."""
    return await _autopilot_month(month_start()), (await _caps(AUTOPILOT)).line


@db_operation("ai.reserve_analyst_read")
def _reserve_analyst(since: datetime, amount: int, cap: int) -> int | None:
    """A read's reservation on the analyst's line, or None when the month's budget
    can't take one more read's ceiling. Under the same lock as every other reservation."""
    with db.atomic():
        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (_LEDGER_LOCK,))
        if cap - _line_sum(ANALYST, since) < amount:
            return None
        return AiSpend.insert(user_id=None, cost_micros=amount, line=ANALYST).execute()


async def reserve_analyst() -> int | None:
    """Reserve one read's ceiling on the analyst's line; None when this month's
    budget for reads is used up (the read is skipped, not failed)."""
    return await _reserve_analyst(month_start(), _micros(settings.analyst_read_max_usd),
                                  (await _caps(ANALYST)).line)


@db_operation("ai.analyst_month_spend")
def _analyst_month(since: datetime) -> int:
    return _line_sum(ANALYST, since)


async def analyst_month_spend() -> tuple[int, int]:
    """This month's spend on the analyst's reads and the line's cap, in micro-dollars."""
    return await _analyst_month(month_start()), (await _caps(ANALYST)).line


@db_operation("ai.cancel_reservation")
def cancel(reservation_id: int) -> None:
    """Drop a reservation whose turn never started (nothing was spent)."""
    AiSpend.delete().where(AiSpend.id == reservation_id, AiSpend.settled == False).execute()  # noqa: E712


@db_operation("ai.hold_reservation")
def hold(reservation_id: int) -> None:
    """Keep a reservation at its ceiling as the charge. For a call that went out
    and never answered: it may be billed, and nothing came back to count."""
    AiSpend.update(settled=True).where(AiSpend.id == reservation_id).execute()


def settle_sync(reservation_id: int, cost: int, turn_id: int | None) -> None:
    """Set a reservation to its turn's real cost. Callers hold the transaction."""
    (AiSpend.update(cost_micros=cost, settled=True, turn_id=turn_id)
     .where(AiSpend.id == reservation_id)
     .execute())


@db_operation("ai.settle_reservation")
def settle(reservation_id: int, cost: int, turn_id: int | None = None) -> None:
    settle_sync(reservation_id, cost, turn_id)
