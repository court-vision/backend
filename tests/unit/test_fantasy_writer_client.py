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


ESPN_IR_BODY = ('{"messages":["Austin Reaves is not eligible for the IL/IR slot, player is not injured."],'
                '"details":[{"message":"Austin Reaves is not eligible for the IL/IR slot, player is not injured.",'
                '"shortMessage":"Austin Reaves is not eligible for the IL/IR slot, player is not injured.",'
                '"resolution":null,"type":"TRAN_ROSTER_INELIGIBLE_IR_NOT_INJURED","metaData":null}]}')


@pytest.mark.unit
def test_409_with_espn_json_excerpt_becomes_prose(writer):
    """The first captured ESPN rejection (2026-09-08): the user must see the sentence, not the dict."""
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED", "http_status": 400,
                                                     "espn_body_excerpt": ESPN_IR_BODY}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert exc.value.message == "Austin Reaves is not eligible for the IL/IR slot, player is not injured."
    assert exc.value.espn_error_code == "TRAN_ROSTER_INELIGIBLE_IR_NOT_INJURED"
    assert exc.value.excerpt == ESPN_IR_BODY  # the raw body is kept for the audit row, not the user


@pytest.mark.unit
@pytest.mark.parametrize("excerpt", [
    '{"details":1}',                       # truthy but not a list -> details[0] used to raise TypeError
    '{"details":{"shortMessage":"x"}}',    # a dict -> details[0] used to raise KeyError
    '{"messages":7}',
    '{"details":[],"messages":[]}',
    '{"details":[{"shortMessage":"","message":"  "}],"messages":[""]}',
])
def test_a_malformed_espn_body_yields_no_prose_instead_of_raising(writer, excerpt):
    """ESPN's error body is not a contract; parsing it must never mask the rejection."""
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED",
                                                     "http_status": 400, "espn_body_excerpt": excerpt}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert exc.value.espn_status == 400 and exc.value.excerpt == excerpt


@pytest.mark.unit
def test_an_empty_short_message_falls_through_to_the_next_candidate(writer):
    excerpt = '{"details":[{"shortMessage":"","message":"Roster is locked."}]}'
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED",
                                                     "http_status": 400, "espn_body_excerpt": excerpt}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert exc.value.message == "Roster is locked."


@pytest.mark.unit
def test_409_prefers_the_writers_parsed_fields(writer):
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED", "http_status": 400,
                                                     "espn_message": "Locked.", "espn_error_code": "TRAN_X",
                                                     "espn_body_excerpt": ESPN_IR_BODY}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_lineup(PAYLOAD))
    assert (exc.value.message, exc.value.espn_error_code) == ("Locked.", "TRAN_X")


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


# ---- transactions -----------------------------------------------------------------------


TXN_PAYLOAD = {"season": 2027, "league_id": 1, "espn_team_id": 4, "member_id": "{S}",
               "credentials": {"espn_s2": "s2", "swid": "{S}"}, "scoring_period_id": 12,
               "add_player_id": 6450, "drop_player_id": 3112335, "idempotency_key": "21:txn:12:v1:6450:3112335"}


@pytest.mark.unit
def test_transaction_posts_the_envelope_to_the_transaction_path(writer):
    result = asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert result.ok and result.espn_status == 200 and result.idempotent_replay is False
    assert len(writer.requests) == 1
    req = writer.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/espn/transaction"
    assert req.headers["Authorization"] == "Bearer tok-123"
    assert json.loads(req.content) == TXN_PAYLOAD                       # add/drop ids, never a `moves` list


@pytest.mark.unit
def test_transaction_409_carries_espn_prose(writer):
    body = ('{"messages":["Roster is full."],"details":[{"shortMessage":"Roster is full.",'
            '"type":"TRAN_ROSTER_FULL","metaData":null}]}')
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED", "http_status": 400,
                                                     "espn_body_excerpt": body}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert (exc.value.message, exc.value.espn_error_code, exc.value.espn_status) == ("Roster is full.", "TRAN_ROSTER_FULL", 400)


@pytest.mark.unit
def test_transaction_409_without_prose_has_its_own_default(writer):
    writer.responses.append(httpx.Response(409, json={"ok": False, "error_code": "ESPN_REJECTED", "http_status": 400}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert exc.value.message == "ESPN rejected the change"


@pytest.mark.unit
def test_transaction_403_422_and_503_map_like_the_lineup_route(writer):
    writer.responses.append(httpx.Response(403, json={"ok": False, "error_code": "ESPN_AUTH_REJECTED", "http_status": 401}))
    with pytest.raises(fw.FantasyWriterAuthRejected):
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))

    writer.responses.append(httpx.Response(422, json={"ok": False, "error_code": "INVALID_TRANSACTION", "detail": "add equals drop"}))
    with pytest.raises(fw.FantasyWriterRejected) as exc:
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert exc.value.message == "add equals drop" and exc.value.espn_status is None

    writer.responses.append(httpx.Response(503, json={"ok": False, "error_code": "ESPN_UPSTREAM_ERROR", "http_status": 503}))
    with pytest.raises(fw.FantasyWriterUnavailable):
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert len(writer.requests) == 3                                     # one request per call, never a retry


@pytest.mark.unit
def test_transaction_connection_failure_is_one_attempt(writer):
    writer.responses.append(httpx.ConnectError("down"))
    with pytest.raises(fw.FantasyWriterUnavailable):
        asyncio.run(fw.apply_transaction(TXN_PAYLOAD))
    assert len(writer.requests) == 1
