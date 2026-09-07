"""
services.fantasy_writer_client over an httpx.MockTransport: the request the
private writer receives (bearer token, JSON payload) and how each of its answers
maps to the typed errors — with exactly ONE request per call, whatever happens.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from services import fantasy_writer_client as fw

PAYLOAD = {"season": 2027, "league_id": 1, "espn_team_id": 4, "member_id": "{S}",
           "credentials": {"espn_s2": "s2", "swid": "{S}"}, "scoring_period_id": 12,
           "moves": [{"player_id": 10, "from_slot_id": 12, "to_slot_id": 11}], "idempotency_key": "k"}


@pytest.fixture
def writer(monkeypatch):
    state = SimpleNamespace(responses=[], requests=[])

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        nxt = state.responses.pop(0) if state.responses else httpx.Response(200, json={"ok": True, "http_status": 200, "espn_body_excerpt": "{}"})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(fw, "client_factory",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://writer.test"))
    monkeypatch.setattr(fw.settings, "fantasy_writer_token", SecretStr("tok-123"))
    return state


@pytest.mark.unit
def test_success_sends_the_payload_with_the_bearer_token(writer):
    result = asyncio.run(fw.apply_lineup(PAYLOAD))
    assert result.ok and result.espn_status == 200 and result.idempotent_replay is False
    assert len(writer.requests) == 1
    req = writer.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/espn/lineup"
    assert req.headers["Authorization"] == "Bearer tok-123"
    assert json.loads(req.content) == PAYLOAD


@pytest.mark.unit
def test_idempotent_replay_is_surfaced(writer):
    writer.responses.append(httpx.Response(200, json={"ok": True, "http_status": 200, "idempotent_replay": True}))
    assert asyncio.run(fw.apply_lineup(PAYLOAD)).idempotent_replay is True


@pytest.mark.unit
def test_409_is_a_rejection_carrying_espn_text(writer):
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED", "http_status": 400,
                                                     "espn_body_excerpt": "Invalid selection"}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert exc.value.espn_status == 400 and exc.value.message == "Invalid selection"


@pytest.mark.unit
def test_403_means_the_cookies_are_dead(writer):
    writer.responses.append(httpx.Response(403, json={"ok": False, "error_code": "ESPN_AUTH_REJECTED", "http_status": 401}))
    with pytest.raises(fw.FantasyWriterAuthRejected):
        asyncio.run(fw.apply_lineup(PAYLOAD))


@pytest.mark.unit
@pytest.mark.parametrize("response", [
    httpx.Response(502, json={"ok": False, "error_code": "ESPN_UPSTREAM_ERROR", "http_status": 503}),
    httpx.Response(504, json={"ok": False, "error_code": "ESPN_TIMEOUT"}),
    httpx.Response(401, json={"detail": "Invalid token"}),
    httpx.Response(200, content=b"not json"),
])
def test_everything_else_is_unavailable(writer, response):
    writer.responses.append(response)
    with pytest.raises(fw.FantasyWriterUnavailable):
        asyncio.run(fw.apply_lineup(PAYLOAD))


@pytest.mark.unit
def test_connection_failure_is_unavailable_after_exactly_one_attempt(writer):
    writer.responses.append(httpx.ConnectError("down"))
    with pytest.raises(fw.FantasyWriterUnavailable):
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert len(writer.requests) == 1  # a lineup POST is never retried


@pytest.mark.unit
def test_422_from_the_writer_is_a_rejection(writer):
    writer.responses.append(httpx.Response(422, json={"ok": False, "error_code": "INVALID_MOVES", "detail": "duplicate player"}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert "duplicate" in exc.value.message
