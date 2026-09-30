"""
AiService.route against a scripted fake client (services/ai/service.py).

What is pinned: the router's request (its own prompt, its two tools, the answer
format on every call, the view in the user turn), that the answer is validated
and the StatMuse link built by the server, that every question reaching the
model is recorded -- failures included -- and that guard rejections are logged
but not recorded. The loop's budgets and error mapping are shared with /ai/ask
and covered in test_ai_service.py.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from core.errors import AppError, ProviderError, ServiceUnavailableError
from core.rate_limit import quota_limiter
from core.settings import settings
from schemas.ai import AiContext
from services.ai import routing, service
from services.ai.prompts import ROUTER_PROMPT
from services.ai.routing import _Found
from services.ai.tools import ROUTER_TOOL_NAMES, ROUTER_TOOLS, ToolOutcome

SENGUN, SABONIS = 1630578, 1627734


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def msg(stop_reason, *content):
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(content), model="claude-opus-5", _request_id=f"req_{stop_reason}",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, iterations=None),
    )


def answer(kind="show", text_="Opening Alperen Sengun's last 15 games", target=None, statmuse_query=None,
           suggestions=(), gap=None, missing=None):
    return text(json.dumps({"kind": kind, "text": text_, "target": target, "statmuse_query": statmuse_query,
                            "suggestions": list(suggestions), "gap": gap, "missing": missing}))


PLAYER_TARGET = {"type": "terminal", "mode": "player", "player_id": SENGUN, "compare_ids": [],
                 "team_id": None, "nba_team": None, "window": "l15"}


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def env(monkeypatch):
    """AI on, a scripted client, stubbed tools, lookups and recording."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))
    monkeypatch.setattr(settings, "ai_max_model_calls", 4)
    monkeypatch.setattr(settings, "ai_max_tool_calls", 8)
    monkeypatch.setattr(settings, "ai_user_daily_limit", 30)
    monkeypatch.setattr(settings, "ai_global_daily_limit", 300)
    monkeypatch.setattr(settings, "ai_request_timeout_seconds", 5.0)
    quota_limiter.storage.reset()

    state = SimpleNamespace(fake=None, tool_calls=[], recorded=[], logged=[],
                            found=_Found(frozenset({SENGUN, SABONIS}), frozenset({7}), frozenset({"HOU"})))

    def install(*steps):
        state.fake = FakeMessages(steps)
        monkeypatch.setattr(service, "get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=state.fake)))
        return state.fake
    state.install = install

    async def fake_run_tool(name, raw, *, ctx=None, allowed=None):
        state.tool_calls.append((name, raw, ctx, allowed))
        return ToolOutcome('{"players": [{"player_id": 1630578, "name": "Alperen Sengun"}]}', is_error=False)
    monkeypatch.setattr(service, "run_tool", fake_run_tool)

    async def fake_lookup(player_ids, team_ids, nba_teams, user_id):
        return state.found
    monkeypatch.setattr(routing, "_lookup", fake_lookup)

    async def fake_record(**row):
        state.recorded.append(row)
        return 17
    monkeypatch.setattr(service.questions, "record", fake_record)

    class Log:
        def info(self, event, **kw):
            state.logged.append((event, kw))
    monkeypatch.setattr(service, "get_logger", lambda: Log())

    yield state
    quota_limiter.storage.reset()


def route(question="how's he been lately", context=None, user_id=42):
    return asyncio.run(service.AiService.route(question, context or AiContext(), user_id=user_id))


@pytest.mark.unit
class TestRequest:
    def test_the_router_request(self, env):
        fake = env.install(msg("tool_use", tool_use("t1", "search_players", {"name": "Sengun"})),
                           msg("end_turn", answer(target=PLAYER_TARGET)))

        route("sengun last 15", AiContext(page="terminal", mode="player", player_id=SABONIS, window="l10"))

        for call in fake.calls:
            assert call["system"] == ROUTER_PROMPT
            assert call["tools"] == ROUTER_TOOLS
            # the answer format rides on every call, or the cached prefix would change
            assert call["output_config"] == {"effort": settings.ai_effort, "format": routing.ANSWER_FORMAT}
        turn = fake.calls[0]["messages"][0]["content"]
        assert turn.startswith('Current view: {"mode": "player", "page": "terminal", "player_id": 1627734, "window": "l10"}')
        assert turn.endswith("Question: sengun last 15")

    def test_tools_run_as_the_caller_within_the_router_toolset(self, env):
        env.install(msg("tool_use", tool_use("t1", "get_my_teams", {})),
                    msg("end_turn", answer(target={"type": "page", "page": "matchup", "team_id": 7, "rankings": None})))

        route("am I winning", user_id=42)

        (name, _, ctx, allowed), = env.tool_calls
        assert (name, ctx.user_id, allowed) == ("get_my_teams", 42, ROUTER_TOOL_NAMES)


@pytest.mark.unit
class TestAnswers:
    def test_show(self, env):
        env.install(msg("end_turn", answer(target=PLAYER_TARGET)))

        data = route().data

        assert data.kind == "show" and data.target.player_id == SENGUN and data.target.window == "l15"
        assert (data.statmuse_url, data.question_id) == (None, 17)

    def test_statmuse_link_is_built_by_the_server(self, env):
        env.install(msg("end_turn", answer(kind="statmuse", text_="StatMuse has this one",
                                           statmuse_query="Alperen Sengun vs Domantas Sabonis rebounds per game this season",
                                           gap="no_view", missing="side-by-side rebounds view")))

        data = route("sengun vs sabonis boards").data

        assert data.kind == "statmuse" and data.target is None
        assert data.statmuse_url == (
            "https://www.statmuse.com/nba/ask/alperen-sengun-vs-domantas-sabonis-rebounds-per-game-this-season")

    def test_a_target_that_fails_validation_becomes_cannot(self, env):
        env.install(msg("end_turn", answer(target={**PLAYER_TARGET, "player_id": 999})))

        data = route().data

        assert (data.kind, data.gap, data.target) == ("cannot", "invalid_target", None)
        assert env.recorded[-1]["gap"] == "invalid_target"

    def test_missing_is_recorded_but_never_returned(self, env):
        env.install(msg("end_turn", answer(kind="cannot", text_="Court Vision doesn't track trade rumors",
                                           suggestions=["Is Sengun injured?"], gap="no_data", missing="news feed")))

        resp = route("any trade rumors on sengun")

        assert "missing" not in resp.data.model_dump()
        assert env.recorded[-1]["missing"] == "news feed"


@pytest.mark.unit
class TestRecording:
    def test_a_routed_question_is_recorded_with_its_spend(self, env):
        env.install(msg("tool_use", tool_use("t1", "search_players", {"name": "Sengun"})),
                    msg("end_turn", answer(target=PLAYER_TARGET)))

        route("sengun last 15", AiContext(mode="player", player_id=SABONIS))

        row = env.recorded[-1]
        assert (row["user_id"], row["question"], row["kind"], row["outcome"]) == (42, "sengun last 15", "show", "ok")
        assert row["context"] == {"mode": "player", "player_id": SABONIS}
        assert row["target"]["player_id"] == SENGUN
        assert (row["model_calls"], row["input_tokens"]) == (2, 200)
        assert row["tool_calls"] == [{"name": "search_players", "input": {"name": "Sengun"}, "is_error": False}]
        assert row["ungrounded_numbers"] == 0

    def test_a_made_up_number_is_counted(self, env):
        env.install(msg("end_turn", answer(text_="Sengun is averaging 21.4 points lately", target=PLAYER_TARGET)))

        route("how's sengun")

        assert env.recorded[-1]["ungrounded_numbers"] == 1
        assert env.logged[-1][1]["ungrounded_numbers"] == 1

    def test_a_failure_after_the_model_ran_is_still_recorded(self, env):
        env.install(msg("refusal"))

        with pytest.raises(AppError):
            route("some question")

        assert env.recorded[-1]["outcome"] == "AI_DECLINED" and env.recorded[-1]["kind"] is None

    def test_an_unreadable_answer_is_recorded_as_incomplete(self, env):
        env.install(msg("end_turn", text("not json")))

        with pytest.raises(ProviderError):
            route()

        assert env.recorded[-1]["outcome"] == "AI_INCOMPLETE"

    def test_a_request_turned_away_by_a_guard_is_logged_not_recorded(self, env, monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", False)

        with pytest.raises(ServiceUnavailableError):
            route()

        assert env.recorded == []
        assert env.logged[-1][1]["outcome"] == "AI_DISABLED"
        assert env.logged[-1][1]["endpoint"] == "route"

    def test_a_question_that_cannot_be_recorded_is_still_answered(self, env, monkeypatch):
        async def failing_record(**row):
            return None
        monkeypatch.setattr(service.questions, "record", failing_record)
        env.install(msg("end_turn", answer(target=PLAYER_TARGET)))

        data = route().data

        assert data.kind == "show" and data.question_id is None


@pytest.mark.unit
def test_recording_never_raises(monkeypatch):
    from services.ai import questions

    async def boom(row):
        raise RuntimeError("database gone")
    monkeypatch.setattr(questions, "_insert", boom)

    assert asyncio.run(questions.record(user_id=1, outcome="ok")) is None
