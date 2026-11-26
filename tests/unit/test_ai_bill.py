"""
The bill check (services/ai/bill.py): Anthropic's Usage and Cost Analytics API read with the Admin
key over a mocked transport, beside a stubbed ledger. No request leaves the test.
"""

from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from core.settings import settings
from services.ai import bill

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 11, 26, 12, 30, tzinfo=timezone.utc)
ADMIN = "sk-ant-admin01-secret-admin-key-AAAA"
APP_KEY = "sk-ant-api03-R2DxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxigAA"


def run(coro):
    import asyncio
    return asyncio.run(coro)


def cost_row(day, key, cents):
    start = f"{day}T00:00:00Z"
    return {"starting_at": start, "ending_at": start, "group": {"api_key_id": key},
            "cost": {"currency": "USD", "amount": cents, "list_amount": cents, "requests": None}}


KEYS = {"data": [
    {"id": "apikey_app", "name": "court-vision-prod", "partial_key_hint": "sk-ant-api03-R2D...igAA"},
    {"id": "apikey_laptop", "name": "laptop evals", "partial_key_hint": "sk-ant-api03-Zzz...0000"},
], "has_more": False, "last_id": "apikey_laptop"}


class Anthropic:
    """A mocked Anthropic: answers by path and range, and keeps every request."""

    def __init__(self):
        self.requests = []
        self.status = {}
        self.pages = {}
        self.error = None

    def handler(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        path = request.url.path
        if path in self.status:
            return httpx.Response(self.status[path], json={"error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}})
        if path == bill.KEYS_PATH:
            return httpx.Response(200, json=KEYS)
        q = parse_qs(request.url.query.decode())
        start = q["starting_at"][0]
        page = q.get("page", [None])[0]
        if "ending_at" not in q:  # today so far
            return httpx.Response(200, json={"data": [cost_row("2026-11-26", "apikey_app", "150.000000")],
                                             "has_more": False, "next_page": None,
                                             "data_refreshed_at": "2026-11-26T09:00:00Z"})
        if start.startswith("2026-10-01"):
            return httpx.Response(200, json={"data": [cost_row("2026-10-30", "apikey_app", "1000.000000"),
                                                      cost_row("2026-10-31", None, "25.500000")],
                                             "has_more": False, "next_page": None,
                                             "data_refreshed_at": "2026-11-26T08:00:00Z"})
        # This month: two pages
        if page is None:
            return httpx.Response(200, json={"data": [cost_row("2026-11-24", "apikey_app", "300.000000")],
                                             "has_more": True, "next_page": "page_2",
                                             "data_refreshed_at": "2026-11-26T08:30:00Z"})
        return httpx.Response(200, json={"data": [cost_row("2026-11-25", "apikey_app", "280.000000"),
                                                  cost_row("2026-11-25", "apikey_laptop", "41.000000")],
                                         "has_more": False, "next_page": None,
                                         "data_refreshed_at": "2026-11-26T08:30:00Z"})


@pytest.fixture
def anthropic(monkeypatch):
    fake = Anthropic()
    monkeypatch.setattr(bill, "client_factory",
                        lambda: httpx.AsyncClient(base_url=bill.BASE_URL, transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(settings, "anthropic_admin_key", SecretStr(ADMIN))
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr(APP_KEY))

    async def ledger(since):
        assert since == datetime(2026, 10, 1, tzinfo=timezone.utc)
        return {date(2026, 10, 30): (9_800_000, 0), date(2026, 11, 25): (2_750_000, 0),
                date(2026, 11, 26): (1_200_000, 1_000_000)}
    monkeypatch.setattr(bill, "_ledger", ledger)
    bill.forget()
    yield fake
    bill.forget()


def test_no_admin_key_says_so_and_asks_anthropic_nothing(anthropic, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_admin_key", None)
    out = run(bill.read(NOW))
    assert out.state == "no_key" and "ANTHROPIC_ADMIN_KEY" in out.message
    assert anthropic.requests == [] and out.months == []


def test_two_months_by_day_beside_the_ledger(anthropic):
    out = run(bill.read(NOW))
    assert out.state == "ok"
    last, this = out.months
    # Cents to micro-dollars: $10.00 + $0.255 in October, the no-key row counted in the total only
    assert (last.month, last.billed_micros, last.ours_micros, last.ledger_micros) == \
        (date(2026, 10, 1), 10_255_000, 10_000_000, 9_800_000)
    assert (this.month, this.billed_micros, this.ours_micros, this.ledger_micros, this.open_micros) == \
        (date(2026, 11, 1), 7_710_000, 7_300_000, 3_950_000, 1_000_000)
    # Every day from October 1 to today, empty ones at zero
    assert out.days[0].day == date(2026, 10, 1) and out.days[-1].day == date(2026, 11, 26)
    assert len(out.days) == 31 + 26
    today = out.days[-1]
    assert (today.billed_micros, today.ours_micros, today.ledger_micros, today.open_micros) == \
        (1_500_000, 1_500_000, 1_200_000, 1_000_000)
    # The earliest refresh of the three reads: complete up to there
    assert out.refreshed_at == datetime(2026, 11, 26, 8, tzinfo=timezone.utc)


def test_this_month_s_keys_are_named_and_the_app_s_own_is_marked(anthropic):
    out = run(bill.read(NOW))
    assert [(k.api_key_id, k.name, k.ours, k.billed_micros) for k in out.keys] == [
        ("apikey_app", "court-vision-prod", True, 7_300_000),
        ("apikey_laptop", "laptop evals", False, 410_000),
    ]


def test_it_reads_with_the_admin_key_the_beta_and_daily_buckets_and_follows_pages(anthropic):
    run(bill.read(NOW))
    costs = [r for r in anthropic.requests if r.url.path == bill.COST_PATH]
    # October, November's two pages, today so far
    assert len(costs) == 4
    assert all(r.headers["x-api-key"] == ADMIN for r in anthropic.requests)
    assert all(r.headers["anthropic-beta"] == bill.BETA for r in costs)
    assert all(APP_KEY not in str(r.headers) for r in anthropic.requests)
    october = parse_qs(costs[0].url.query.decode())
    assert october["starting_at"] == ["2026-10-01T00:00:00Z"] and october["ending_at"] == ["2026-11-01T00:00:00Z"]
    assert october["time_bucket_width"] == ["1d"] and october["group_by[]"] == ["api_key_id"]
    assert parse_qs(costs[2].url.query.decode())["page"] == ["page_2"]
    today = parse_qs(costs[3].url.query.decode())
    assert today["starting_at"] == ["2026-11-26T00:00:00Z"] and "ending_at" not in today
    keys = [r for r in anthropic.requests if r.url.path == bill.KEYS_PATH]
    assert len(keys) == 1 and "anthropic-beta" not in keys[0].headers


@pytest.mark.parametrize("status, state", [(401, "refused"), (403, "refused"), (404, "unavailable"),
                                           (500, "unreachable")])
def test_a_refusal_is_its_state_with_anthropic_s_words(anthropic, status, state):
    anthropic.status[bill.COST_PATH] = status
    out = run(bill.read(NOW))
    assert out.state == state and out.months == []
    assert out.message == f"Anthropic answered {status}: invalid x-api-key"
    assert ADMIN not in out.message


def test_anthropic_not_answering_is_unreachable(anthropic):
    anthropic.error = httpx.ConnectTimeout("timed out")
    out = run(bill.read(NOW))
    assert out.state == "unreachable" and out.message == "Anthropic didn't answer (ConnectTimeout)"


def test_keys_that_can_t_be_listed_leave_the_bill_without_names(anthropic):
    anthropic.status[bill.KEYS_PATH] = 403
    out = run(bill.read(NOW))
    assert out.state == "ok" and out.months[1].billed_micros == 7_710_000
    assert out.months[1].ours_micros is None and out.days[-1].ours_micros is None
    assert all(k.name is None and not k.ours for k in out.keys)


def test_an_answer_is_kept_a_few_minutes_and_a_failure_one(anthropic, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(bill._clock, "monotonic", lambda: clock[0])
    run(bill.read(NOW))
    asked = len(anthropic.requests)
    clock[0] += bill.KEEP_OK_S - 1
    run(bill.read(NOW))
    assert len(anthropic.requests) == asked
    clock[0] += 2
    run(bill.read(NOW))
    assert len(anthropic.requests) == 2 * asked
    # A refusal is kept a minute, not five
    bill.forget()
    anthropic.status[bill.COST_PATH] = 401
    run(bill.read(NOW))
    failed = len(anthropic.requests)
    clock[0] += bill.KEEP_FAILED_S + 1
    run(bill.read(NOW))
    assert len(anthropic.requests) == failed + 1


def test_on_the_first_before_1_am_only_last_month_is_read(anthropic):
    out = run(bill.read(datetime(2026, 11, 1, 0, 20, tzinfo=timezone.utc)))
    costs = [r for r in anthropic.requests if r.url.path == bill.COST_PATH]
    assert len(costs) == 1 and parse_qs(costs[0].url.query.decode())["starting_at"] == ["2026-10-01T00:00:00Z"]
    assert out.months[1].month == date(2026, 11, 1) and out.months[1].billed_micros == 0


@pytest.mark.parametrize("hint, ours", [("sk-ant-api03-R2D...igAA", True), ("sk-ant-api03-R2D...igAB", False),
                                        ("sk-ant-api03-R2D", False), (None, False), ("...igAA", False)])
def test_the_app_s_key_is_found_by_its_hint(monkeypatch, hint, ours):
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr(APP_KEY))
    assert bill._is_ours(hint) is ours
