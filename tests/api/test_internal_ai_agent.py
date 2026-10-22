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


CHIP = {
    "proposal_id": 31, "state": "proposed", "kind": "transaction", "team_id": 22, "day": 2, "date": "2026-10-22",
    "dow": "Thu", "scoring_period_id": 3,
    "add": {"espn_id": 6450, "nba_player_id": 202695, "name": "Kawhi Leonard", "team": "LAC"},
    "drop": {"espn_id": 4594268, "nba_player_id": 1630162, "name": "Anthony Edwards", "team": "MIN"},
    "summary": "Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it.",
    "reason": "4 games left", "change": 1.0, "scoring": "categories",
    "expires_at": "2026-10-22T23:00:00Z", "sent_at": None,
}


@pytest.mark.api
def test_a_proposal_is_read_back_with_a_fresh_check(authed_client, user, invited, monkeypatch):
    from schemas.ai import AiProposalCheckData, AiProposalChip
    seen = []

    async def proposal(proposal_id, user_id):
        seen.append((proposal_id, user_id))
        return AiProposalCheckData(chip=AiProposalChip(**CHIP), problems=["Kawhi Leonard is on waivers until Thu"])
    monkeypatch.setattr(AgentService, "proposal", staticmethod(proposal))

    r = authed_client.get(f"{BASE}/proposals/31")

    assert r.status_code == 200 and seen == [(31, 42)]
    data = r.json()["data"]
    assert data["problems"] == ["Kawhi Leonard is on waivers until Thu"]
    assert data["chip"]["add"] == CHIP["add"] and data["chip"]["state"] == "proposed" and data["chip"]["date"] == "2026-10-22"
    assert "type" not in data["chip"]


@pytest.mark.api
def test_a_proposal_is_dismissed_and_a_sent_one_refuses(authed_client, user, invited, monkeypatch):
    from schemas.ai import AiProposalChip
    seen = []

    async def dismiss(proposal_id, user_id):
        seen.append((proposal_id, user_id))
        if proposal_id == 32:
            raise ConflictError("AI_PROPOSAL_SENT", "This move was already sent")
        return AiProposalChip(**{**CHIP, "state": "dismissed"})
    monkeypatch.setattr(AgentService, "dismiss_proposal", staticmethod(dismiss))

    r = authed_client.post(f"{BASE}/proposals/31/dismiss")
    assert r.status_code == 200 and r.json()["data"]["state"] == "dismissed"
    r = authed_client.post(f"{BASE}/proposals/32/dismiss")
    assert (r.status_code, r.json()["error_code"]) == (409, "AI_PROPOSAL_SENT")
    assert seen == [(31, 42), (32, 42)]
    assert authed_client.get(f"{BASE}/proposals/0").status_code == 422


@pytest.mark.api
def test_a_turn_s_chip_is_a_block_in_the_conversation(authed_client, user, invited, monkeypatch):
    from schemas.ai import AiTurnView
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    async def detail(conversation_id, user_id):
        turn = AiTurnView(id=9, created_at=now, message="Who should I add?", outcome="ok", cost_usd=0.01,
                          parts=[{"kind": "block", "block": {"type": "proposal", "id": "p1", **CHIP, "state": "sent",
                                                            "sent_at": "2026-10-21T20:00:00Z"}},
                                 {"kind": "text", "text": "Kawhi for Edwards."}])
        return AiConversationDetail(id=5, title="Who should I add?", team_id=22, created_at=now, updated_at=now,
                                    turns=1, full=False, history=[turn])
    monkeypatch.setattr(AgentService, "detail", staticmethod(detail))

    parts = authed_client.get(f"{BASE}/conversations/5").json()["data"]["history"][0]["parts"]

    assert parts[0]["block"]["type"] == "proposal" and parts[0]["block"]["id"] == "p1"
    assert parts[0]["block"]["state"] == "sent" and parts[0]["block"]["drop"]["espn_id"] == 4594268
    assert parts[1] == {"kind": "text", "text": "Kawhi for Edwards."}
