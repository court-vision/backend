"""
/v1/internal/teams/{id}/lineup[/plan|/moves] over the test app: ownership via the
usual `ensure_team_owned` stub, the credential loader stubbed at the route, and
the service replaced per test so every documented status is exercised:
200 (board / Yahoo empty state), 403 ROSTER_WRITE_DISABLED, 409 ROSTER_STALE
with the fresh board, 422 ROSTER_MOVE_INVALID with codes, 409 ROSTER_WRITE_REJECTED,
503 ROSTER_WRITE_UNAVAILABLE, and FastAPI's own 422 for a malformed body.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import lineup_editor as routes
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupState, LineupStateResp
from services import lineup_editor_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")
YAHOO = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027, yahoo_team_key="k")

STATE = LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=4, nba_date="2026-10-20",
                    scoring_period_id=1, scoring_period_source="provider", slot_counts={"11": 1, "12": 1},
                    players=[], can_write=False, write_blocked_reason="writes_disabled",
                    roster_version="abc", fetched_at="now")

BODY = {"moves": [{"player_id": 11, "from_slot_id": 12, "to_slot_id": 11}], "expected_scoring_period_id": 1,
        "roster_version": "abc"}


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


def _stub(monkeypatch, name, outcome):
    async def fake(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(svc.LineupEditorService, name, staticmethod(fake))


@pytest.mark.api
def test_get_lineup_returns_the_board(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, "read_state", LineupStateResp(status=ApiStatus.SUCCESS, message="ok", data=STATE))
    r = authed_client.get("/v1/internal/teams/7/lineup")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["roster_version"] == "abc" and body["data"]["can_write"] is False
    assert body["data"]["write_blocked_reason"] == "writes_disabled"


@pytest.mark.api
def test_yahoo_team_is_an_empty_success(authed_client, owned, monkeypatch):
    _league(monkeypatch, YAHOO)
    _stub(monkeypatch, "read_state", LineupStateResp(status=ApiStatus.SUCCESS, message=svc.NOT_ESPN_MESSAGE, data=None))
    r = authed_client.get("/v1/internal/teams/7/lineup")
    assert r.status_code == 200 and r.json()["data"] is None


@pytest.mark.api
def test_unowned_team_is_404(authed_client, owned, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: None)
    assert authed_client.get("/v1/internal/teams/8/lineup").status_code == 404


@pytest.mark.api
@pytest.mark.parametrize("error, status, code", [
    (svc.RosterWriteDisabled(), 403, "ROSTER_WRITE_DISABLED"),
    (svc.RosterWriteBlocked(data={"reason": "not_team_owner"}), 409, "ROSTER_WRITE_BLOCKED"),
    (svc.RosterWriteRejected(message="Invalid Selection.", data={"espn_status": 400}), 409, "ROSTER_WRITE_REJECTED"),
    (svc.RosterWriteUnavailable(), 503, "ROSTER_WRITE_UNAVAILABLE"),
])
def test_write_failures_have_real_statuses(authed_client, owned, monkeypatch, error, status, code):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, "apply_manual", error)
    r = authed_client.post("/v1/internal/teams/7/lineup/moves", json=BODY)
    assert r.status_code == status
    assert r.json()["error_code"] == code and r.headers["X-Error-Code"] == code


@pytest.mark.api
def test_stale_carries_the_fresh_board(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, "apply_manual", svc.RosterStale(data={"lineup": STATE.model_dump(mode="json")}))
    r = authed_client.post("/v1/internal/teams/7/lineup/moves", json=BODY)
    assert r.status_code == 409
    assert r.json()["error_code"] == "ROSTER_STALE" and r.json()["data"]["lineup"]["roster_version"] == "abc"


@pytest.mark.api
def test_invalid_moves_carry_codes(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, "apply_manual", svc.RosterMoveInvalid(data={"errors": [{"player_id": 11, "code": "LOCKED", "message": "m"}]}))
    r = authed_client.post("/v1/internal/teams/7/lineup/moves", json=BODY)
    assert r.status_code == 422 and r.json()["data"]["errors"][0]["code"] == "LOCKED"


@pytest.mark.api
def test_malformed_body_is_rejected_before_the_service(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    called = []
    async def fake(*a, **k):
        called.append(1)
    monkeypatch.setattr(svc.LineupEditorService, "apply_manual", staticmethod(fake))
    r = authed_client.post("/v1/internal/teams/7/lineup/moves", json={**BODY, "moves": []})
    assert r.status_code == 422 and called == []


@pytest.mark.api
def test_plan_route(authed_client, owned, monkeypatch):
    from schemas.lineup_editor import LineupPlanData, LineupPlanResp
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, "plan_today", LineupPlanResp(status=ApiStatus.SUCCESS, message="1 move",
                                                     data=LineupPlanData(summary="1 move", roster_version="abc")))
    r = authed_client.get("/v1/internal/teams/7/lineup/plan")
    assert r.status_code == 200 and r.json()["data"]["summary"] == "1 move"
