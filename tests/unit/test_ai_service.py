"""
The AI layer's request path (services/ai/service.py, guards.py) against a
scripted fake client: no network, no API key.

What is pinned here is the cost and safety envelope, not answer quality:
the kill switch and quotas run before any model call, the loop cannot exceed
its model-call or tool-call budget, the last call cannot use tools, refusals
and truncations fail loudly, fallback boundaries are honoured when echoing,
SDK errors land on the app's error taxonomy, and every request logs its spend.
"""

import asyncio
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from core.errors import AppError, ProviderError, ProviderTimeout, ServiceUnavailableError
from core.rate_limit import quota_limiter
from core.settings import Settings, settings
from services.ai import service
from services.ai.tools import ToolOutcome


# ---- scripted responses ----

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def fallback_marker():
    return SimpleNamespace(type="fallback")


def msg(stop_reason, *content, model="claude-opus-5", iterations=None,
        input_tokens=100, output_tokens=20, cache_read=0, cache_write=0):
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(content), model=model, _request_id=f"req_{stop_reason}",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                              iterations=iterations),
    )


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        # Snapshot: the loop appends to `messages` after the call returns
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))
    monkeypatch.setattr(settings, "ai_max_model_calls", 4)
    monkeypatch.setattr(settings, "ai_max_tool_calls", 8)
    monkeypatch.setattr(settings, "ai_user_daily_limit", 30)
    monkeypatch.setattr(settings, "ai_global_daily_limit", 300)
    monkeypatch.setattr(settings, "ai_request_timeout_seconds", 5.0)
    quota_limiter.storage.reset()
    yield
    quota_limiter.storage.reset()


@pytest.fixture
def script(monkeypatch, ai_on):
    """Install a scripted client; returns the FakeMessages to inspect."""
    holder = {}

    def install(*steps):
        fake = FakeMessages(steps)
        monkeypatch.setattr(service, "get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
        holder["fake"] = fake
        return fake
    return install


@pytest.fixture
def tool_log(monkeypatch):
    """Stub the tools: record calls, answer with a tiny JSON body."""
    calls = []

    async def fake_run_tool(name, raw):
        calls.append((name, raw))
        return ToolOutcome('{"ok": true}', is_error=False)
    monkeypatch.setattr(service, "run_tool", fake_run_tool)
    return calls


@pytest.fixture
def logged(monkeypatch):
    events = []

    class Log:
        def info(self, event, **kw):
            events.append((event, kw))
    monkeypatch.setattr(service, "get_logger", lambda: Log())
    return events


def ask(question="How is Sengun doing?", user_id=42):
    return asyncio.run(service.AiService.ask(question, user_id=user_id))


# ---- guards ----

@pytest.mark.unit
class TestGuards:
    def test_off_by_default_and_before_any_model_call(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", False)
        monkeypatch.setattr(service, "get_client", lambda: pytest.fail("no model call while disabled"))

        with pytest.raises(ServiceUnavailableError) as exc:
            ask()
        assert exc.value.error_code == "AI_DISABLED"

    def test_the_user_quota_is_per_user(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_user_daily_limit", 2)
        script(*[msg("end_turn", text("ok")) for _ in range(3)])

        ask(user_id=1)
        ask(user_id=1)
        with pytest.raises(AppError) as exc:
            ask(user_id=1)
        assert (exc.value.error_code, exc.value.status_code) == ("AI_QUOTA_EXCEEDED", 429)
        ask(user_id=2)  # someone else is unaffected

    def test_the_global_budget_caps_everyone(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_global_daily_limit", 1)
        script(msg("end_turn", text("ok")))

        ask(user_id=1)
        with pytest.raises(ServiceUnavailableError) as exc:
            ask(user_id=2)
        assert exc.value.error_code == "AI_DAILY_BUDGET_REACHED"

    def test_enabled_without_a_key_does_not_boot(self):
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            Settings(ai_enabled=True, anthropic_api_key=None)

    def test_unknown_effort_does_not_boot(self):
        with pytest.raises(ValidationError, match="ai_effort"):
            Settings(ai_effort="turbo")


# ---- the loop ----

@pytest.mark.unit
class TestLoop:
    def test_tool_round_trip(self, script, tool_log, logged):
        fake = script(
            msg("tool_use", text("Looking him up."), tool_use("t1", "search_players", {"name": "Sengun"})),
            msg("end_turn", text("Sengun is averaging 21.4 points.")),
        )

        resp = ask()

        assert resp.data.answer == "Sengun is averaging 21.4 points."
        assert tool_log == [("search_players", {"name": "Sengun"})]
        assert [(c.name, c.is_error) for c in resp.data.tool_calls] == [("search_players", False)]
        second = fake.calls[1]["messages"]
        assert second[1]["role"] == "assistant"
        assert second[2]["content"] == [
            {"type": "tool_result", "tool_use_id": "t1", "content": '{"ok": true}', "is_error": False}
        ]

    def test_request_shape(self, script, tool_log, logged):
        fake = script(msg("end_turn", text("ok")))

        ask()

        call = fake.calls[0]
        assert call["model"] == settings.ai_model
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"] == {"effort": settings.ai_effort}
        assert call["fallbacks"] == "default"
        assert call["betas"] == ["server-side-fallback-2026-07-01"]
        assert call["cache_control"] == {"type": "ephemeral"}
        assert "tool_choice" not in call

    def test_the_last_allowed_call_cannot_use_tools(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_max_model_calls", 3)
        fake = script(
            msg("tool_use", tool_use("t1", "search_players", {"name": "a"})),
            msg("tool_use", tool_use("t2", "search_players", {"name": "b"})),
            msg("end_turn", text("Best answer with what I found.")),
        )

        resp = ask()

        assert len(fake.calls) == 3
        assert [c.get("tool_choice") for c in fake.calls] == [None, None, {"type": "none"}]
        assert resp.data.usage.model_calls == 3

    def test_never_more_model_calls_than_the_budget(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_max_model_calls", 2)
        # A model that ignores tool_choice=none still cannot buy a third call
        fake = script(
            msg("tool_use", tool_use("t1", "search_players", {"name": "a"})),
            msg("tool_use", text("Partial."), tool_use("t2", "search_players", {"name": "b"})),
        )

        resp = ask()

        assert len(fake.calls) == 2
        assert resp.data.answer == "Partial."
        # ...nor run tools whose results no later call could read
        assert tool_log == [("search_players", {"name": "a"})]
        assert [c.input for c in resp.data.tool_calls] == [{"name": "a"}]

    def test_the_tool_budget_spans_turns_and_refuses_the_excess(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_max_tool_calls", 3)
        fake = script(
            msg("tool_use", *[tool_use(f"t{i}", "get_player_status", {"player_id": i}) for i in range(1, 3)]),
            msg("tool_use", *[tool_use(f"t{i}", "get_player_status", {"player_id": i}) for i in range(3, 6)]),
            msg("end_turn", text("done")),
        )

        resp = ask()

        assert len(tool_log) == 3  # 2 + 1; t4 and t5 never ran
        refused = fake.calls[2]["messages"][-1]["content"][-2:]
        assert [r["tool_use_id"] for r in refused] == ["t4", "t5"]
        assert all(r["is_error"] and r["content"] == service.BUDGET_SPENT for r in refused)
        assert fake.calls[2]["tool_choice"] == {"type": "none"}  # budget spent -> final call
        assert sum(c.is_error for c in resp.data.tool_calls) == 2

    def test_usage_sums_across_calls(self, script, tool_log, logged):
        script(
            msg("tool_use", tool_use("t1", "search_players", {"name": "a"}), input_tokens=900, cache_write=700),
            msg("end_turn", text("ok"), input_tokens=300, output_tokens=40, cache_read=700),
        )

        usage = ask().data.usage

        assert (usage.input_tokens, usage.output_tokens) == (1200, 60)
        assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (700, 700)
        assert usage.model_calls == 2


# ---- failures ----

@pytest.mark.unit
class TestFailures:
    def test_a_refusal_after_fallback_is_an_error_not_an_answer(self, script, tool_log, logged):
        script(msg("refusal"))

        with pytest.raises(AppError) as exc:
            ask()
        assert (exc.value.error_code, exc.value.status_code) == ("AI_DECLINED", 422)

    def test_truncation_is_an_error(self, script, tool_log, logged):
        script(msg("max_tokens", text("Sengun is averag")))

        with pytest.raises(ProviderError) as exc:
            ask()
        assert exc.value.error_code == "AI_INCOMPLETE"

    def test_an_empty_answer_is_an_error(self, script, tool_log, logged):
        script(msg("end_turn"))

        with pytest.raises(ProviderError) as exc:
            ask()
        assert exc.value.error_code == "AI_INCOMPLETE"

    def test_the_request_deadline(self, script, tool_log, logged, monkeypatch):
        monkeypatch.setattr(settings, "ai_request_timeout_seconds", 0.05)

        async def slow_run_tool(name, raw):
            await asyncio.sleep(1)
        monkeypatch.setattr(service, "run_tool", slow_run_tool)
        script(msg("tool_use", tool_use("t1", "search_players", {"name": "a"})))

        with pytest.raises(ProviderTimeout):
            ask()
        assert logged[-1][1]["outcome"] == "AI_TIMEOUT"

    @pytest.mark.parametrize("status, expected_type, code", [
        (429, ProviderError, "AI_BUSY"),
        (529, ProviderError, "AI_UNAVAILABLE"),
        (401, ServiceUnavailableError, "AI_MISCONFIGURED"),
        (400, AppError, "AI_REQUEST_REJECTED"),
    ])
    def test_sdk_errors_map_to_the_taxonomy(self, script, tool_log, logged, status, expected_type, code):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx2.Response(status, request=request, json={"error": {"message": "x"}})
        error_cls = {429: anthropic.RateLimitError, 401: anthropic.AuthenticationError,
                     400: anthropic.BadRequestError}.get(status, anthropic.InternalServerError)
        script(error_cls("x", response=response, body=None))

        with pytest.raises(expected_type) as exc:
            ask()
        assert exc.value.error_code == code

    def test_connection_errors_and_timeouts(self, script, tool_log, logged):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        script(anthropic.APITimeoutError(request=request))
        with pytest.raises(ProviderTimeout):
            ask()

        script(anthropic.APIConnectionError(request=request))
        with pytest.raises(ProviderError) as exc:
            ask()
        assert exc.value.error_code == "AI_UNAVAILABLE"


# ---- fallbacks ----

@pytest.mark.unit
class TestFallbackBoundaries:
    def test_blocks_before_the_boundary_keep_only_text(self):
        declined_call = tool_use("t0", "search_players", {"name": "x"})
        thinking = SimpleNamespace(type="thinking", thinking="")
        content = [text("partial "), declined_call, thinking, fallback_marker(), text("rest"),
                   tool_use("t1", "search_players", {"name": "y"})]

        kept = service._served_content(content)

        assert [b.type for b in kept] == ["text", "text", "tool_use"]
        assert kept[-1].id == "t1"

    def test_no_boundary_keeps_everything(self):
        content = [SimpleNamespace(type="thinking", thinking=""), text("a"), tool_use("t1", "x", {})]
        assert service._served_content(content) == content

    def test_a_fallback_is_reported(self, script, tool_log, logged):
        script(msg("end_turn", fallback_marker(), text("Answered by the fallback."), model="claude-opus-4-8",
                   iterations=[SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]))

        resp = ask()

        assert resp.data.answer == "Answered by the fallback."
        assert resp.data.usage.fallback is True
        assert resp.data.usage.model == "claude-opus-4-8"


# ---- the log line ----

@pytest.mark.unit
class TestLogging:
    def test_success_logs_spend(self, script, tool_log, logged):
        script(
            msg("tool_use", tool_use("t1", "search_players", {"name": "a"})),
            msg("end_turn", text("ok")),
        )

        ask(user_id=7)

        event, fields = logged[-1]
        assert event == "ai_request"
        assert fields["outcome"] == "ok"
        assert fields["user_id"] == 7
        assert fields["model_calls"] == 2
        assert fields["tool_calls"] == ["search_players"]
        assert fields["input_tokens"] == 200
        assert fields["request_ids"] == ["req_tool_use", "req_end_turn"]

    def test_a_failure_still_logs_what_it_spent(self, script, tool_log, logged):
        script(msg("tool_use", tool_use("t1", "search_players", {"name": "a"})), msg("refusal"))

        with pytest.raises(AppError):
            ask()

        fields = logged[-1][1]
        assert fields["outcome"] == "AI_DECLINED"
        assert fields["model_calls"] == 2
        assert fields["input_tokens"] == 200

    def test_the_question_is_not_logged(self, script, tool_log, logged):
        script(msg("end_turn", text("ok")))

        ask(question="my secret league strategy")

        assert "my secret league strategy" not in repr(logged)
