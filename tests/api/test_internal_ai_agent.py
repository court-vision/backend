"""
The AI desk's routes over the test app: off by default, invite-only, errors
before the stream as ordinary JSON, and the turn itself as server-sent events.
The turn loop is covered in tests/unit/test_ai_agent.py; here the service is
replaced outright.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.errors import ConflictError
from core.settings import settings
from schemas.ai import AiConversationDetail
from services.ai import agent
from services.ai.agent_service import AgentService

BASE = "/v1/internal/ai"


@pytest.fixture
def user(monkeypatch):
    from services import user_sync_service
    row = MagicMock(); row.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: row))


@pytest.fixture
def invited(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_agent_user_ids", [42])


def _sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = [line for line in chunk.split("\n") if not line.startswith(":")]
        if not lines:
            continue
        name = lines[0].removeprefix("event: ")
        events.append((name, json.loads(lines[1].removeprefix("data: "))))
    return events


@pytest.mark.api
def test_off_by_default(authed_client, user, monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", False)

    assert authed_client.post(f"{BASE}/conversations", json={}).json()["error_code"] == "AI_DISABLED"
    assert authed_client.get(f"{BASE}/usage").status_code == 503


@pytest.mark.api
def test_invite_only(authed_client, user, monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_agent_user_ids", [7])

    r = authed_client.post(f"{BASE}/conversations", json={})
    assert (r.status_code, r.json()["error_code"]) == (403, "AI_INVITE_ONLY")
    r = authed_client.post(f"{BASE}/conversations/1/turns", json={"message": "hi"})
    assert (r.status_code, r.json()["error_code"]) == (403, "AI_INVITE_ONLY")


@pytest.mark.api
def test_usage_says_whether_the_caller_is_invited(authed_client, user, invited, monkeypatch):
    from services.ai import spend

    async def month(user_id):
        return spend.Spend(user=1_250_000, total=2_000_000, user_cap=100_000_000, total_cap=100_000_000)
    monkeypatch.setattr(spend, "month_spend", month)

    data = authed_client.get(f"{BASE}/usage").json()["data"]

    assert data == {"access": True, "month_usd": 1.25, "cap_usd": 100.0, "model": "claude-opus-5-5", "effort": "medium"}


@pytest.mark.api
def test_create_takes_an_optional_team(authed_client, user, invited, monkeypatch):
    seen = []

    async def create(user_id, team_id):
        seen.append((user_id, team_id))
        now = datetime(2026, 10, 13, tzinfo=timezone.utc)
        return AiConversationDetail(id=5, title=None, team_id=team_id, created_at=now, updated_at=now,
                                    turns=0, full=False, history=[])
    monkeypatch.setattr(AgentService, "create", staticmethod(create))

    assert authed_client.post(f"{BASE}/conversations").json()["data"]["id"] == 5
    assert authed_client.post(f"{BASE}/conversations", json={"team_id": 22}).json()["data"]["team_id"] == 22
    assert seen == [(42, None), (42, 22)]


@pytest.mark.api
def test_a_turn_streams_its_events_in_order(authed_client, user, invited, monkeypatch):
    seen = []

    async def start(conversation_id, user_id, message):
        seen.append((conversation_id, user_id, message))
        emit = agent.Emitter()
        emit("status", {"text": "Reading your matchup", "kind": "tool", "tool": "get_matchup"})
        emit("text", {"delta": "You trail in blocks."})
        emit("block", {"type": "table", "id": "t1", "rows": [["Jalen Duren", 1.8]]})
        emit("done", {"turn_id": 9, "outcome": "ok"})
        emit.close()
        return emit
    monkeypatch.setattr(AgentService, "start_turn", staticmethod(start))

    r = authed_client.post(f"{BASE}/conversations/3/turns", json={"message": "How are my blocks?"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert [name for name, _ in _sse(r.text)] == ["status", "text", "block", "done"]
    assert _sse(r.text)[2][1]["rows"] == [["Jalen Duren", 1.8]]
    assert seen == [(3, 42, "How are my blocks?")]


@pytest.mark.api
def test_errors_before_the_stream_are_json(authed_client, user, invited, monkeypatch):
    async def busy(conversation_id, user_id, message):
        raise ConflictError("AI_CONVERSATION_BUSY", "This conversation is still answering")
    monkeypatch.setattr(AgentService, "start_turn", staticmethod(busy))

    r = authed_client.post(f"{BASE}/conversations/3/turns", json={"message": "again"})

    assert (r.status_code, r.json()["error_code"]) == (409, "AI_CONVERSATION_BUSY")


@pytest.mark.api
@pytest.mark.parametrize("body", [{"message": ""}, {"message": "x" * 2001}, {"message": "a\x00b"}, {}])
def test_bad_messages_are_refused_before_anything_runs(authed_client, user, invited, monkeypatch, body):
    async def never(*a):
        raise AssertionError("must not start")
    monkeypatch.setattr(AgentService, "start_turn", staticmethod(never))

    assert authed_client.post(f"{BASE}/conversations/3/turns", json=body).status_code == 422
