"""
POST /v1/internal/ai/ask over the test app: the kill switch as the default,
request validation, the success envelope, and the error codes a frontend will
branch on. The loop itself is covered in tests/unit/test_ai_service.py; here
the service is either left to its guards or replaced outright.
"""

from unittest.mock import MagicMock

import pytest

from core.errors import AppError
from core.settings import settings
from schemas.ai import AiToolCall, AiUsage, AskData, AskResp, RouteData, RouteResp, TerminalTarget
from schemas.common import ApiStatus
from services.ai import questions
from services.ai.service import AiService

URL = "/v1/internal/ai/ask"

OK = AskResp(status=ApiStatus.SUCCESS, message="Answered", data=AskData(
    answer="Sengun is averaging 21.4 points over his last 10 games.",
    tool_calls=[AiToolCall(name="search_players", input={"name": "Sengun"}, is_error=False)],
    usage=AiUsage(model="claude-opus-5", model_calls=2, input_tokens=1800, output_tokens=120,
                  cache_read_input_tokens=700, cache_creation_input_tokens=700, fallback=False),
))


@pytest.fixture
def user(monkeypatch):
    from services import user_sync_service
    row = MagicMock(); row.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: row))


def _stub(monkeypatch, outcome):
    calls = []

    async def fake(question, *, user_id):
        calls.append((question, user_id))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(AiService, "ask", staticmethod(fake))
    return calls


@pytest.mark.api
def test_off_by_default(authed_client, user, monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", False)

    r = authed_client.post(URL, json={"question": "How is Sengun doing?"})

    assert r.status_code == 503
    assert r.json()["error_code"] == "AI_DISABLED"


@pytest.mark.api
def test_answer_envelope(authed_client, user, monkeypatch):
    calls = _stub(monkeypatch, OK)

    r = authed_client.post(URL, json={"question": "How is Sengun doing?"})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["data"]["answer"].startswith("Sengun")
    assert body["data"]["tool_calls"][0]["name"] == "search_players"
    assert body["data"]["usage"]["model_calls"] == 2
    assert calls == [("How is Sengun doing?", 42)]


@pytest.mark.api
@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 501}])
def test_question_is_validated_before_the_service(authed_client, user, monkeypatch, payload):
    calls = _stub(monkeypatch, OK)

    r = authed_client.post(URL, json=payload)

    assert r.status_code == 422
    assert calls == []


@pytest.mark.api
@pytest.mark.parametrize("error, status", [
    (AppError("AI_QUOTA_EXCEEDED", "used up", status_code=429), 429),
    (AppError("AI_DECLINED", "declined", status_code=422), 422),
])
def test_service_errors_keep_their_codes(authed_client, user, monkeypatch, error, status):
    _stub(monkeypatch, error)

    r = authed_client.post(URL, json={"question": "q"})

    assert r.status_code == status
    assert r.json()["error_code"] == error.error_code


@pytest.mark.api
def test_declares_its_response_schema(app):
    op = app.openapi()["paths"][URL]["post"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/AskResp")


# ---- /ai/route and feedback ----

ROUTE_URL = "/v1/internal/ai/route"
USAGE = AiUsage(model="claude-opus-5", model_calls=2, input_tokens=900, output_tokens=60,
                cache_read_input_tokens=700, cache_creation_input_tokens=700, fallback=False)
ROUTED = RouteResp(status=ApiStatus.SUCCESS, message="Routed", data=RouteData(
    kind="show", text="Opening Alperen Sengun's last 15 games",
    target=TerminalTarget(mode="player", player_id=1630578, window="l15"),
    question_id=17, usage=USAGE,
))


def _stub_route(monkeypatch, outcome):
    calls = []

    async def fake(question, context, *, user_id):
        calls.append((question, context.model_dump(exclude_defaults=True), user_id))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(AiService, "route", staticmethod(fake))
    return calls


@pytest.mark.api
def test_route_is_off_by_default(authed_client, user, monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", False)

    r = authed_client.post(ROUTE_URL, json={"question": "sengun last 15"})

    assert r.status_code == 503
    assert r.json()["error_code"] == "AI_DISABLED"


@pytest.mark.api
def test_route_envelope(authed_client, user, monkeypatch):
    calls = _stub_route(monkeypatch, ROUTED)

    r = authed_client.post(ROUTE_URL, json={"question": "sengun last 15",
                                            "context": {"mode": "player", "player_id": 1627734}})

    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["kind"], data["target"]["type"], data["target"]["window"]) == ("show", "terminal", "l15")
    assert data["question_id"] == 17
    assert calls == [("sengun last 15", {"mode": "player", "player_id": 1627734}, 42)]


@pytest.mark.api
@pytest.mark.parametrize("payload", [
    {"question": ""},
    {"question": "x" * 501},
    {"question": "q", "context": {"window": "l83"}},
    {"question": "q", "context": {"compare_ids": [1, 2, 3, 4, 5]}},
    {"question": "q", "context": {"nba_team": "houston"}},
])
def test_route_validates_before_the_service(authed_client, user, monkeypatch, payload):
    calls = _stub_route(monkeypatch, ROUTED)

    r = authed_client.post(ROUTE_URL, json=payload)

    assert r.status_code == 422
    assert calls == []


@pytest.mark.api
def test_feedback_on_your_own_question(authed_client, user, monkeypatch):
    seen = []

    async def fake(question_id, user_id, feedback):
        seen.append((question_id, user_id, feedback))
        return True
    monkeypatch.setattr(questions, "set_feedback", fake)

    r = authed_client.post("/v1/internal/ai/questions/17/feedback", json={"feedback": "up"})

    assert r.status_code == 200
    assert r.json()["data"] == {"question_id": 17, "feedback": "up"}
    assert seen == [(17, 42, "up")]


@pytest.mark.api
def test_feedback_on_someone_elses_question_reads_as_missing(authed_client, user, monkeypatch):
    async def fake(question_id, user_id, feedback):
        return False
    monkeypatch.setattr(questions, "set_feedback", fake)

    r = authed_client.post("/v1/internal/ai/questions/99/feedback", json={"feedback": "down"})

    assert r.status_code == 404
    assert r.json()["error_code"] == "AI_QUESTION_NOT_FOUND"


@pytest.mark.api
@pytest.mark.parametrize("payload", [{"feedback": "meh"}, {}])
def test_feedback_values(authed_client, user, monkeypatch, payload):
    async def fake(question_id, user_id, feedback):
        return True
    monkeypatch.setattr(questions, "set_feedback", fake)

    assert authed_client.post("/v1/internal/ai/questions/17/feedback", json=payload).status_code == 422


@pytest.mark.api
@pytest.mark.parametrize("path, ref", [
    ("/v1/internal/ai/route", "RouteResp"),
    ("/v1/internal/ai/questions/{question_id}/feedback", "AiFeedbackResp"),
])
def test_router_routes_declare_their_response_schemas(app, path, ref):
    op = app.openapi()["paths"][path]["post"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(f"/{ref}")
