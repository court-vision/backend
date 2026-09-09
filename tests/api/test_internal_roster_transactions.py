"""
POST /v1/internal/teams/{id}/roster/transactions over the test app: ownership
via the usual `ensure_team_owned` stub, the credential loader stubbed at the
route, and the service replaced per test so every documented status is
exercised — 200, 404, 403 ROSTER_WRITE_DISABLED, 409 ROSTER_WRITE_BLOCKED /
ROSTER_STALE / ROSTER_WRITE_REJECTED, 422 ROSTER_TRANSACTION_INVALID with
`data.reason`, 503 ROSTER_WRITE_UNAVAILABLE, and FastAPI's own 422.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import roster_transactions as routes
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupState
from schemas.roster_transaction import RosterTransactionData, RosterTransactionPlayer, RosterTransactionReq, RosterTransactionResp
from services import lineup_editor_service as editor
from services import roster_transaction_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")

STATE = LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=4, nba_date="2026-10-20",
                    scoring_period_id=1, scoring_period_source="provider", slot_counts={"11": 1, "12": 1},
                    players=[], can_write=True, roster_version="v2", fetched_at="now")

BODY = {"add_player_id": 6450, "drop_player_id": 4594268, "expected_scoring_period_id": 1, "roster_version": "v1"}

OK = RosterTransactionResp(status=ApiStatus.SUCCESS, message="Added Kawhi Leonard, Dropped Anthony Edwards — sent to ESPN",
                           data=RosterTransactionData(
                               lineup=STATE, verified=True, audit_id=9, scoring_period_id=1,
                               added=RosterTransactionPlayer(player_id=6450, name="Kawhi Leonard", team="LAC"),
                               dropped=RosterTransactionPlayer(player_id=4594268, name="Anthony Edwards", team="MIN")))


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


def _league(monkeypatch, league_info):
    async def hydrate(team):
        return league_info
    monkeypatch.setattr(routes, "load_owned_league_info", hydrate)


def _stub(monkeypatch, outcome):
    calls = []

    async def fake(team, league_info, req):
        calls.append((team, league_info, req))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(svc.RosterTransactionService, "apply", staticmethod(fake))
    return calls


@pytest.mark.api
def test_applied_transaction_returns_the_board_and_both_players(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, OK)

    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json=BODY)

    assert r.status_code == 200
    team, league_info, req = calls[0]
    assert team.team_id == 7 and team.user_id == 42 and league_info == ESPN
    assert req == RosterTransactionReq(**BODY)
    body = r.json()
    assert body["status"] == "success" and body["message"].startswith("Added Kawhi Leonard")
    d = body["data"]
    assert (d["verified"], d["audit_id"], d["scoring_period_id"]) == (True, 9, 1)
    assert d["lineup"]["roster_version"] == "v2" and d["lineup"]["can_write"] is True
    assert d["added"] == {"player_id": 6450, "name": "Kawhi Leonard", "team": "LAC"}
    assert d["dropped"] == {"player_id": 4594268, "name": "Anthony Edwards", "team": "MIN"}


@pytest.mark.api
def test_unowned_team_is_404(authed_client, owned, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: None)
    calls = _stub(monkeypatch, OK)
    r = authed_client.post("/v1/internal/teams/8/roster/transactions", json=BODY)
    assert r.status_code == 404 and r.json()["error_code"] == "TEAM_NOT_FOUND" and calls == []


@pytest.mark.api
@pytest.mark.parametrize("error, status, code", [
    (editor.RosterWriteDisabled(), 403, "ROSTER_WRITE_DISABLED"),
    (editor.RosterWriteBlocked(message=svc.NOT_ESPN_MESSAGE, data={"reason": "provider_not_supported"}), 409, "ROSTER_WRITE_BLOCKED"),
    (editor.RosterWriteRejected(message="Roster is full.", data={"espn_status": 400, "espn_error_code": "TRAN_ROSTER_FULL"}), 409, "ROSTER_WRITE_REJECTED"),
    (editor.RosterWriteUnavailable(), 503, "ROSTER_WRITE_UNAVAILABLE"),
])
def test_write_failures_have_real_statuses(authed_client, owned, monkeypatch, error, status, code):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, error)
    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json=BODY)
    assert r.status_code == status
    body = r.json()
    assert body["error_code"] == code and r.headers["X-Error-Code"] == code and body["message"] == error.message


@pytest.mark.api
def test_stale_carries_the_fresh_board(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, editor.RosterStale(data={"lineup": STATE.model_dump(mode="json")}))
    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json=BODY)
    assert r.status_code == 409
    assert r.json()["error_code"] == "ROSTER_STALE" and r.json()["data"]["lineup"]["roster_version"] == "v2"


@pytest.mark.api
def test_invalid_transaction_is_422_with_a_reason(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, svc.RosterTransactionInvalid(message="Kawhi Leonard is on waivers until 2026-10-25 — place the claim on ESPN",
                                                    data={"reason": "add_on_waivers", "player_id": 6450}))
    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json=BODY)
    assert r.status_code == 422 and r.headers["X-Error-Code"] == "ROSTER_TRANSACTION_INVALID"
    body = r.json()
    assert body["status"] == "validation_error" and body["error_code"] == "ROSTER_TRANSACTION_INVALID"
    assert body["data"]["reason"] == "add_on_waivers" and body["data"]["player_id"] == 6450
    assert "waivers" in body["message"]


@pytest.mark.api
@pytest.mark.parametrize("bad", [
    {**BODY, "add_player_id": 0},
    {**BODY, "roster_version": ""},
    {k: v for k, v in BODY.items() if k != "expected_scoring_period_id"},
])
def test_malformed_body_is_rejected_before_the_service(authed_client, owned, monkeypatch, bad):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, OK)
    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json=bad)
    assert r.status_code == 422 and calls == []


@pytest.mark.api
def test_add_or_drop_alone_is_a_valid_body(authed_client, owned, monkeypatch):
    """Whether at least one is given is the service's call (422 nothing_to_do), not the schema's."""
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, OK)
    r = authed_client.post("/v1/internal/teams/7/roster/transactions", json={"expected_scoring_period_id": 1, "roster_version": "v1"})
    assert r.status_code == 200 and calls[0][2].add_player_id is None and calls[0][2].drop_player_id is None
