"""
The live rankings' analyst (services/ai/analyst.py) against a scripted client:
no network, no API key, no database.

What is pinned here is the envelope, not the judgment: nothing is asked while
it is off or out of budget, a read is one model call with the fixed prompt and
the answer's schema, every read is reserved and settled to what it cost,
refusals and unreadable answers are outcomes and not errors, a failure the API
answered with hands its reservation back, and a read that went out and never
answered keeps it.
"""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from core.errors import ProviderError, ProviderTimeout, ServiceUnavailableError
from core.settings import settings
from schemas.analyst import AnalystReadReq
from services.ai import analyst, client, service, spend


def text(t):
    return SimpleNamespace(type="text", text=t)


def msg(stop_reason, *content, model="claude-opus-5-5", input_tokens=2_000, output_tokens=500,
        cache_read=0, cache_write=0, iterations=None):
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(content), model=model, _request_id="req_1",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                              iterations=iterations),
    )


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return await step()
        return step


class FakeClient:
    """The client as `_create` uses it: the messages, and the options a call set for itself."""

    def __init__(self, messages):
        self.beta = SimpleNamespace(messages=messages)
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self


class Ledger:
    """services.ai.spend's analyst line, in memory."""

    def __init__(self, room=True):
        self.room = room
        self.reserved, self.settled, self.cancelled, self.held = [], {}, [], []

    async def reserve_analyst(self):
        if not self.room:
            return None
        self.reserved.append(len(self.reserved) + 1)
        return self.reserved[-1]

    async def settle(self, reservation_id, cost, turn_id=None):
        self.settled[reservation_id] = cost

    async def cancel(self, reservation_id):
        self.cancelled.append(reservation_id)

    async def hold(self, reservation_id):
        self.held.append(reservation_id)

    async def analyst_month_spend(self):
        return sum(self.settled.values()) + 300_000 * len(self.held), 40_000_000


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "analyst_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))
    monkeypatch.setattr(settings, "analyst_model", "claude-opus-5-5")
    monkeypatch.setattr(settings, "analyst_effort", "medium")
    monkeypatch.setattr(settings, "analyst_read_timeout_seconds", 5.0)


@pytest.fixture
def ledger(monkeypatch):
    book = Ledger()
    for name in ("reserve_analyst", "settle", "cancel", "hold", "analyst_month_spend"):
        monkeypatch.setattr(spend, name, getattr(book, name))
    return book


@pytest.fixture
def script(monkeypatch, on, ledger):
    def install(*steps):
        fake = FakeMessages(steps)
        fake.client = FakeClient(fake)
        monkeypatch.setattr(service, "get_client", lambda: fake.client)
        return fake
    return install


def request(**over) -> AnalystReadReq:
    body = {
        "season": "2026-27",
        "as_of": "2026-11-16",
        "player_id": 7,
        "name": "Luka Dončić",
        "team": "LAL",
        "reasons": ["A new injury item since his last read"],
        "projection": {"out_now": True, "return_date": None, "minutes_usual": 35.5},
        "items": [{"ref": "news:91", "kind": "injury", "citable": True, "source": "espn_player_news",
                   "at": "2026-11-15 18:02", "text": "Dončić (calf) will be re-evaluated in two weeks."}],
        "bounds": {"minutes_max": 38, "usage_min": 0.6, "usage_max": 1.4, "review_by": "2026-12-07"},
    }
    return AnalystReadReq.model_validate({**body, **over})


def answer(**over) -> str:
    body = {
        "decision": "write", "reason": "ESPN on Nov 15: re-evaluated in two weeks, so a return in early December.",
        "kind": "injury_current", "return_date": "2026-12-08", "return_earliest": "2026-11-30",
        "return_latest": "2026-12-20", "out_for_season": False, "minutes": None, "usage": None,
        "until_date": None, "until_player_id": None, "confidence": "likely", "review_date": "2026-11-30",
        "cites": ["news:91"], "against_owner_note": None,
    }
    return json.dumps({**body, **over})


def read(req=None):
    return asyncio.run(analyst.read(req or request()))


@pytest.mark.unit
class TestThePrompt:
    def test_it_shares_the_playbooks_rules_for_reading_a_player(self):
        rules = analyst.reading_a_player()
        assert "Minutes and role are the steadiest signals" in rules
        assert "The value ends when the starter returns" in rules
        assert "Categories leagues" not in rules
        assert rules in analyst.ANALYST_PROMPT

    def test_nothing_in_it_changes_from_one_read_to_the_next(self):
        # A cached prefix: the day and the player are the user turn, never the prompt
        for volatile in ("2026", "Dončić", "LAL"):
            assert volatile not in analyst.ANALYST_PROMPT

    def test_every_field_of_the_answer_is_required_and_nothing_else_is_allowed(self):
        schema = analyst.ANSWER_SCHEMA
        assert sorted(schema["required"]) == sorted(schema["properties"])
        assert schema["additionalProperties"] is False
        assert schema["properties"]["decision"]["enum"] == ["write", "keep", "retire", "none"]

    def test_a_playbook_without_the_section_adds_nothing(self):
        assert analyst.reading_a_player("Before a move:\n- check the sample") == ""


@pytest.mark.unit
class TestARead:
    def test_one_call_with_the_fixed_prompt_and_the_players_facts_as_the_turn(self, script):
        fake = script(msg("end_turn", text(answer())))

        data = read()

        assert data.outcome == "read"
        assert data.prompt_version == analyst.PROMPT_VERSION
        assert data.proposal.decision == "write"
        assert data.proposal.return_date == date(2026, 12, 8)
        assert data.proposal.cites == ["news:91"]
        (call,) = fake.calls
        assert call["model"] == "claude-opus-5-5"
        assert call["system"][0]["text"] == analyst.ANALYST_PROMPT
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["output_config"] == {
            "effort": "medium", "format": {"type": "json_schema", "schema": analyst.ANSWER_SCHEMA},
        }
        assert call["thinking"] == {"type": "adaptive"}
        assert call["fallbacks"] == "default" and service.FALLBACK_BETA in call["betas"]
        assert "tools" not in call and "tool_choice" not in call
        turn = json.loads(call["messages"][0]["content"])
        assert turn["name"] == "Luka Dončić" and turn["as_of"] == "2026-11-16"
        assert turn["items"][0]["ref"] == "news:91"
        # Names as written, so the model can name him back
        assert "Dončić" in call["messages"][0]["content"]

    def test_it_is_reserved_before_the_call_and_settled_to_what_it_cost(self, script, ledger):
        script(msg("end_turn", text(answer()), input_tokens=2_000, output_tokens=500, cache_read=1_000))

        data = read()

        # 2,000 in at $4, 500 out at $20, 1,000 cache reads at $0.20 a million
        assert data.usage.cost_micros == 8_000 + 10_000 + 200
        assert ledger.settled == {1: 18_200} and ledger.cancelled == []
        assert data.month_spent_micros == 18_200 and data.month_cap_micros == 40_000_000

    def test_nothing_to_write_is_an_answer(self, script):
        script(msg("end_turn", text(answer(decision="none", return_date=None, return_earliest=None,
                                           return_latest=None, review_date=None, cites=[]))))

        data = read()

        assert data.outcome == "read" and data.proposal.decision == "none"
        assert data.proposal.return_date is None

    def test_a_declined_request_is_an_outcome_and_still_costs_what_it_cost(self, script, ledger):
        script(msg("refusal", input_tokens=2_000, output_tokens=0))

        data = read()

        assert data.outcome == "declined" and data.proposal is None
        assert ledger.settled == {1: 8_000}

    @pytest.mark.parametrize("body", [
        "not json at all",
        json.dumps({"decision": "write"}),
        answer(decision="promote"),
        answer(return_date="early December"),
        answer(confidence="certain"),
    ])
    def test_an_answer_that_is_not_a_proposal_is_malformed_not_an_error(self, script, ledger, body):
        script(msg("end_turn", text(body)))

        data = read()

        assert data.outcome == "malformed" and data.proposal is None
        assert "could not be read" in data.detail
        assert list(ledger.settled) == [1]

    def test_an_answer_that_was_cut_off_is_malformed(self, script):
        script(msg("max_tokens", text('{"decision": "wri')))

        data = read()

        assert data.outcome == "malformed" and data.detail == "the answer was cut off"

    def test_a_fallback_models_text_is_the_answer_and_the_declined_blocks_are_not(self, script):
        entries = [SimpleNamespace(type="message", output_tokens=0), SimpleNamespace(type="fallback_message")]
        script(msg("end_turn", SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="fallback"),
                   text(answer()), model="claude-opus-5", iterations=entries))

        data = read()

        assert data.outcome == "read" and data.usage.fallback is True
        assert data.usage.model == "claude-opus-5"
        # Priced at the model that served it: $5 in, $25 out
        assert data.usage.cost_micros == 10_000 + 12_500


@pytest.mark.unit
class TestWhenNothingIsAsked:
    def test_off_while_the_ai_layer_is_off(self, monkeypatch, on, ledger):
        monkeypatch.setattr(settings, "ai_enabled", False)
        monkeypatch.setattr(service, "get_client", lambda: pytest.fail("no model call while off"))

        data = read()

        assert data.outcome == "off" and ledger.reserved == []

    def test_off_by_its_own_switch(self, monkeypatch, on, ledger):
        monkeypatch.setattr(settings, "analyst_enabled", False)
        monkeypatch.setattr(service, "get_client", lambda: pytest.fail("no model call while off"))

        assert read().outcome == "off"

    def test_out_of_budget_asks_nothing(self, monkeypatch, on, ledger):
        ledger.room = False
        monkeypatch.setattr(service, "get_client", lambda: pytest.fail("no model call past the budget"))

        data = read()

        assert data.outcome == "budget" and data.proposal is None and data.usage is None
        assert data.month_cap_micros == 40_000_000


def _status_error(cls, status):
    request_ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("no", response=httpx2.Response(status, request=request_), body=None)


def _transport_error(cls, cause):
    """The SDK's error for a transport failure, raised from it as the SDK raises it."""
    error = cls(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    error.__cause__ = cause
    return error


@pytest.mark.unit
class TestWhenTheProviderFails:
    """A failed read raises either way. What differs is the reservation: handed
    back only when the API answered, so nothing can have been billed."""

    @pytest.mark.parametrize("error", [
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.BadRequestError, 400),
    ], ids=["busy", "unavailable", "rejected"])
    def test_an_error_the_api_answered_with_hands_the_reservation_back(self, script, ledger, error):
        script(error)

        with pytest.raises(Exception):
            read()

        assert ledger.cancelled == [1] and ledger.held == [] and ledger.settled == {}

    def test_a_call_that_never_started_hands_the_reservation_back(self, monkeypatch, on, ledger):
        monkeypatch.setattr(settings, "anthropic_api_key", None)
        monkeypatch.setattr(client, "_client", None)

        with pytest.raises(ServiceUnavailableError):
            read()

        assert ledger.cancelled == [1] and ledger.held == []

    @pytest.mark.parametrize("error", [
        _transport_error(anthropic.APIConnectionError, httpx2.ConnectError("Name or service not known")),
        _transport_error(anthropic.APIConnectionError, httpx2.ConnectError("Connection refused")),
        _transport_error(anthropic.APITimeoutError, httpx2.ConnectTimeout("timed out")),
        _transport_error(anthropic.APITimeoutError, httpx2.PoolTimeout("no connection free")),
    ], ids=["unresolved", "refused", "connect_timeout", "no_connection_free"])
    def test_a_connection_that_never_opened_hands_the_reservation_back(self, script, ledger, error):
        # Nothing reached the API, so nothing can be billed: an outage must not use up the month
        script(error)

        with pytest.raises((ProviderError, ProviderTimeout)):
            read()

        assert ledger.cancelled == [1] and ledger.held == [] and ledger.settled == {}

    @pytest.mark.parametrize("error", [
        _transport_error(anthropic.APIConnectionError, httpx2.ReadError("connection reset")),
        _transport_error(anthropic.APIConnectionError, httpx2.RemoteProtocolError("server disconnected")),
        _transport_error(anthropic.APIConnectionError, httpx2.WriteError("broken pipe")),
        _transport_error(anthropic.APITimeoutError, httpx2.ReadTimeout("timed out")),
    ], ids=["reset", "disconnected", "write_broke", "read_timeout"])
    def test_a_connection_that_broke_once_open_keeps_the_reservation(self, script, ledger, error):
        script(error)

        with pytest.raises((ProviderError, ProviderTimeout)):
            read()

        assert ledger.held == [1] and ledger.cancelled == [] and ledger.settled == {}

    def test_a_read_is_one_attempt(self, script):
        # A retry inside the client would hide a first attempt that went out and never answered
        fake = script(msg("end_turn", text(answer())))

        read()

        assert fake.client.options == [{"max_retries": 0}] and len(fake.calls) == 1

    def test_a_dropped_connection_keeps_the_reservation(self, script, ledger):
        request_ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        script(anthropic.APIConnectionError(request=request_))

        with pytest.raises(ProviderError) as exc:
            read()

        assert exc.value.error_code == "AI_UNAVAILABLE"
        assert ledger.held == [1] and ledger.cancelled == [] and ledger.settled == {}

    def test_a_client_timeout_keeps_the_reservation(self, script, ledger):
        request_ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        script(anthropic.APITimeoutError(request=request_))

        with pytest.raises(ProviderTimeout):
            read()

        assert ledger.held == [1] and ledger.cancelled == []

    def test_a_read_that_runs_too_long_times_out_and_keeps_its_reservation(self, script, ledger, monkeypatch):
        # The model accepted the request and is still working when we stop
        # listening: it may be billed, so the month counts it at the ceiling
        monkeypatch.setattr(settings, "analyst_read_timeout_seconds", 0.01)

        async def slow():
            await asyncio.sleep(1)

        script(slow)

        with pytest.raises(ProviderTimeout):
            read()

        assert ledger.held == [1] and ledger.cancelled == [] and ledger.settled == {}
        assert asyncio.run(spend.analyst_month_spend())[0] == 300_000

    def test_the_reads_deadline_ends_a_slow_read_before_the_client_would_retry_it(self, script, monkeypatch):
        monkeypatch.setattr(settings, "analyst_read_timeout_seconds", 75.0)
        fake = script(msg("end_turn", text(answer())))

        read()

        timeout = fake.calls[0]["timeout"]
        assert timeout.read > settings.analyst_read_timeout_seconds
        # Connecting has a short one of its own, well inside the deadline
        assert timeout.connect == analyst.CONNECT_TIMEOUT_SECONDS < settings.analyst_read_timeout_seconds


@pytest.mark.unit
class TestTheMonth:
    def test_what_the_reads_have_spent_and_may(self, on, ledger, monkeypatch):
        monkeypatch.setattr(settings, "analyst_read_max_usd", 0.30)
        ledger.settled[1] = 1_250_000

        data = asyncio.run(analyst.month())

        assert data.enabled is True
        assert (data.month_spent_micros, data.month_cap_micros, data.read_max_micros) == (1_250_000, 40_000_000, 300_000)
        assert data.model == "claude-opus-5-5" and data.prompt_version == analyst.PROMPT_VERSION
