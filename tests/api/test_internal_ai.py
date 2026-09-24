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
from schemas.ai import AiToolCall, AiUsage, AskData, AskResp
from schemas.common import ApiStatus
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
