"""
The bill check: what Anthropic bills the organization, beside the ledger (usr.ai_spend), for the
data platform's AI spend page (step 3 of the AI spend dashboard plan).

The ledger prices each call as it happens, from its tokens (services/ai/spend.py). Anthropic's
Usage and Cost Analytics API (beta) is what the organization is billed, before credits. It is read
with the Admin API key (ANTHROPIC_ADMIN_KEY), which can't send a message, over plain HTTP: the
installed SDK has no analytics client yet.

- Two calendar months (UTC), last month and this one: each is one request in daily buckets up to
  today's midnight, and today so far is one more (a daily bucket rounds today away).
- Grouped by API key, so the app's own key (found by Anthropic's partial hint of each key) reads
  apart from any other (a laptop's evals, the Console's workbench).
- Anthropic's data lands within about 4 hours, up to 24: past `refreshed_at` its days read short.
- An answer is kept 5 minutes (a failure 1), so a page left open doesn't poll Anthropic.

Nothing here spends: no model call is made.
"""

from __future__ import annotations

import time as _clock
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

import httpx

from core.logging import get_logger
from core.settings import settings
from db.base import db, db_operation
from schemas.ai_spend import AiBill, AiBillDay, AiBillKey, AiBillMonth, BillState
from services.ai import spend

BASE_URL = "https://api.anthropic.com"
VERSION = "2026-10-21"
BETA = "analytics-usage-cost-2026-08-24"
COST_PATH = "/v1/organizations/analytics/cost"
KEYS_PATH = "/v1/organizations/api_keys"
TIMEOUT_S = 20.0
KEEP_OK_S = 300.0
KEEP_FAILED_S = 60.0
# Pages one request may follow (a page holds up to 1,000 rows: one per key and day)
_MAX_PAGES = 20
# Anthropic's amounts are cents; the ledger's are micro-dollars
_MICROS_PER_CENT = 10_000

# Tests hand in an httpx.AsyncClient over an httpx.MockTransport
client_factory: Optional[Callable[[], httpx.AsyncClient]] = None
_kept: Optional[tuple[float, AiBill]] = None

_LEDGER_SQL = """
SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
       COALESCE(SUM(cost_micros) FILTER (WHERE settled), 0),
       COALESCE(SUM(cost_micros) FILTER (WHERE NOT settled), 0)
FROM usr.ai_spend
WHERE created_at >= %s
GROUP BY 1
"""


class _Failed(Exception):
    def __init__(self, state: BillState, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


def _client() -> httpx.AsyncClient:
    if client_factory is not None:
        return client_factory()
    return httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(TIMEOUT_S, connect=5.0))


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _said(response: httpx.Response) -> str:
    """Anthropic's own words for a refusal (its error message never carries the key)."""
    message = None
    try:
        error = response.json().get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"][:200]
    except (ValueError, AttributeError):
        pass
    return f"Anthropic answered {response.status_code}" + (f": {message}" if message else "")


async def _get(client: httpx.AsyncClient, key: str, path: str, params: list[tuple[str, str]], *,
               beta: bool) -> dict[str, Any]:
    headers = {"x-api-key": key, "anthropic-version": VERSION}
    if beta:
        headers["anthropic-beta"] = BETA
    try:
        response = await client.get(path, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise _Failed("unreachable", f"Anthropic didn't answer ({type(exc).__name__})") from None
    if response.status_code in (401, 403):
        raise _Failed("refused", _said(response))
    if response.status_code == 404:
        raise _Failed("unavailable", _said(response))
    if response.status_code >= 400:
        raise _Failed("unreachable", _said(response))
    try:
        body = response.json()
    except ValueError:
        raise _Failed("unreachable", "Anthropic's answer wasn't JSON") from None
    if not isinstance(body, dict):
        raise _Failed("unreachable", "Anthropic's answer wasn't an object")
    return body


async def _cost(client: httpx.AsyncClient, key: str, starting_at: datetime, ending_at: Optional[datetime],
                ) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Every cost row from `starting_at`, by API key: by UTC day up to `ending_at`, or (with none)
    one row a key up to now."""
    params = [("starting_at", _iso(starting_at)), ("group_by[]", "api_key_id"), ("limit", "1000")]
    if ending_at is not None:
        params += [("ending_at", _iso(ending_at)), ("time_bucket_width", "1d"),
                   ("order_by", "time"), ("order", "asc")]
    rows: list[dict[str, Any]] = []
    refreshed: Optional[str] = None
    page: Optional[str] = None
    for _ in range(_MAX_PAGES):
        body = await _get(client, key, COST_PATH, params + ([("page", page)] if page else []), beta=True)
        rows += [r for r in body.get("data") or [] if isinstance(r, dict)]
        refreshed = body.get("data_refreshed_at") or refreshed
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            break
    return rows, refreshed


async def _keys(client: httpx.AsyncClient, key: str) -> Optional[list[dict[str, Any]]]:
    """The organization's API keys (names and partial hints), or None when they can't be listed:
    the bill still reads, only without names."""
    out: list[dict[str, Any]] = []
    after: Optional[str] = None
    try:
        for _ in range(_MAX_PAGES):
            body = await _get(client, key, KEYS_PATH,
                              [("limit", "1000")] + ([("after_id", after)] if after else []), beta=False)
            out += [k for k in body.get("data") or [] if isinstance(k, dict)]
            after = body.get("last_id")
            if not body.get("has_more") or not after:
                break
    except _Failed as exc:
        get_logger().warning("ai_bill_keys_unread", state=exc.state, message=exc.message)
        return None
    return out


def _is_ours(hint: Optional[str]) -> bool:
    """Whether Anthropic's partial hint of a key ("sk-ant-api03-R2D...igAA") is this app's key."""
    if not hint or "..." not in hint or settings.anthropic_api_key is None:
        return False
    head, tail = hint.split("...", 1)
    secret = settings.anthropic_api_key.get_secret_value()
    return bool(head) and bool(tail) and secret.startswith(head) and secret.endswith(tail)


def _micros(amount: Any) -> int:
    """Anthropic's amount (a decimal string in cents) in micro-dollars; 0 when it isn't one."""
    try:
        return int((Decimal(str(amount)) * _MICROS_PER_CENT).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def _day_of(row: dict[str, Any], default: date) -> date:
    try:
        return datetime.fromisoformat(str(row["starting_at"]).replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (KeyError, ValueError):
        return default


def _parse(at: Optional[str]) -> Optional[datetime]:
    if not at:
        return None
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None


@db_operation("ai.bill_ledger")
def _ledger(since: datetime) -> dict[date, tuple[int, int]]:
    """The ledger by UTC day: settled spend, and reservations still open."""
    return {day: (int(settled), int(held)) for day, settled, held in db.execute_sql(_LEDGER_SQL, (since,)).fetchall()}


def _month_before(month: datetime) -> datetime:
    return (month - timedelta(days=1)).replace(day=1)


async def _read(now: datetime) -> AiBill:
    if settings.anthropic_admin_key is None:
        return AiBill(state="no_key", read_at=now,
                      message="ANTHROPIC_ADMIN_KEY isn't set on the backend's service")
    key = settings.anthropic_admin_key.get_secret_value()
    this_month = spend.month_start(now)
    last_month = _month_before(this_month)
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    rows: list[tuple[date, Optional[str], int]] = []
    refreshed: list[str] = []
    try:
        async with _client() as client:
            # Daily buckets: last month whole, then this month up to today's midnight
            for start, end in ((last_month, this_month), (this_month, today)):
                if end <= start:
                    continue
                found, at = await _cost(client, key, start, end)
                rows += [(_day_of(r, start.date()), (r.get("group") or {}).get("api_key_id"),
                          _micros((r.get("cost") or {}).get("amount"))) for r in found]
                if at:
                    refreshed.append(at)
            # Today so far, one row a key (a range rounds down to the hour: none before 1 AM UTC)
            if now - today >= timedelta(hours=1):
                found, at = await _cost(client, key, today, None)
                rows += [(today.date(), (r.get("group") or {}).get("api_key_id"),
                          _micros((r.get("cost") or {}).get("amount"))) for r in found]
                if at:
                    refreshed.append(at)
            keys = await _keys(client, key)
    except _Failed as exc:
        get_logger().warning("ai_bill_unread", state=exc.state, message=exc.message)
        return AiBill(state=exc.state, message=exc.message, read_at=now)

    ledger = await _ledger(last_month)
    by_id = {k.get("id"): k for k in keys or []}
    ours_ids = {k_id for k_id, k in by_id.items() if _is_ours(k.get("partial_key_hint"))}
    known = bool(ours_ids)

    days = []
    day = last_month.date()
    while day <= today.date():
        billed = sum(m for d, _k, m in rows if d == day)
        ours = sum(m for d, k, m in rows if d == day and k in ours_ids) if known else None
        settled, held = ledger.get(day, (0, 0))
        days.append(AiBillDay(day=day, billed_micros=billed, ours_micros=ours, ledger_micros=settled,
                              open_micros=held))
        day += timedelta(days=1)

    months = []
    for start, end in ((last_month.date(), this_month.date()), (this_month.date(), None)):
        inside = [d for d in days if d.day >= start and (end is None or d.day < end)]
        months.append(AiBillMonth(
            month=start, billed_micros=sum(d.billed_micros for d in inside),
            ours_micros=sum(d.ours_micros or 0 for d in inside) if known else None,
            ledger_micros=sum(d.ledger_micros for d in inside), open_micros=sum(d.open_micros for d in inside)))

    month_keys: dict[Optional[str], int] = {}
    for d, k, m in rows:
        if d >= this_month.date():
            month_keys[k] = month_keys.get(k, 0) + m
    key_rows = sorted(
        (AiBillKey(api_key_id=k_id, name=(by_id.get(k_id) or {}).get("name"),
                   hint=(by_id.get(k_id) or {}).get("partial_key_hint"), ours=k_id in ours_ids, billed_micros=m)
         for k_id, m in month_keys.items()),
        key=lambda k: -k.billed_micros)

    parsed = [p for p in (_parse(at) for at in refreshed) if p is not None]
    bill = AiBill(state="ok", read_at=now, refreshed_at=min(parsed) if parsed else None,
                  months=months, days=days, keys=key_rows)
    get_logger().info("ai_bill_read", month_billed_micros=months[-1].billed_micros,
                      month_ours_micros=months[-1].ours_micros, month_ledger_micros=months[-1].ledger_micros,
                      keys=len(key_rows), names=keys is not None)
    return bill


async def read(now: Optional[datetime] = None) -> AiBill:
    """The bill check as of now: Anthropic's numbers for last month and this one beside the
    ledger's, kept a few minutes. Never raises for Anthropic's answer: `state` says what happened."""
    global _kept
    if _kept is not None:
        at, kept = _kept
        if _clock.monotonic() - at < (KEEP_OK_S if kept.state == "ok" else KEEP_FAILED_S):
            return kept
    bill = await _read(now or datetime.now(timezone.utc))
    _kept = (_clock.monotonic(), bill) if bill.state != "no_key" else None
    return bill


def forget() -> None:
    """Drop the kept answer (tests)."""
    global _kept
    _kept = None
