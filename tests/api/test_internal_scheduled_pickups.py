"""
/v1/internal/teams/{id}/pickups over the test app: ownership via the usual
`ensure_team_owned` stub, the credential loader stubbed at the route, and the service
replaced per test so every documented status is exercised — 200, 404, 403
ROSTER_WRITE_DISABLED, 409 ROSTER_WRITE_BLOCKED / SCHEDULED_PICKUP_DUPLICATE /
SCHEDULED_PICKUP_NOT_PENDING / SCHEDULED_PICKUP_IN_PROGRESS, 422
SCHEDULED_PICKUP_INVALID with `data.reason`, 400 SCORING_PERIOD_OUT_OF_RANGE, and
FastAPI's own 422.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import scheduled_pickups as routes
from core.errors import BadRequestError, NotFoundError
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.scheduled_pickup import (
    ScheduledPickup,
    ScheduledPickupListData,
    ScheduledPickupListResp,
    ScheduledPickupPlayer,
    ScheduledPickupResp,
    SchedulePickupReq,
)
from services import lineup_editor_service as editor
from services import scheduled_pickup_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")
AT = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)

PICKUP = ScheduledPickup(
    id=5, team_id=7, scoring_period_id=3, nba_date=date(2026, 10, 22), status="pending",
    add=ScheduledPickupPlayer(player_id=6450, name="Kawhi Leonard", team="LAC", nba_player_id=202695),
    drop=ScheduledPickupPlayer(player_id=4594268, name="Anthony Edwards", team="MIN"),
    not_before_at=AT, deadline_at=AT, created_at=AT,
)
BODY = {"add_player_id": 6450, "drop_player_id": 4594268, "scoring_period_id": 3}
OK = ScheduledPickupResp(status=ApiStatus.SUCCESS, message="Pickup of Kawhi Leonard scheduled for ESPN day 3", data=PICKUP)
LISTED = ScheduledPickupListResp(status=ApiStatus.SUCCESS, message="1 pending pickup(s)",
                                 data=ScheduledPickupListData(pending=[PICKUP], recent=[]))


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))

    async def hydrate(team):
        return ESPN
    monkeypatch.setattr(routes, "load_owned_league_info", hydrate)


def _stub(monkeypatch, method, outcome):
    calls = []

    async def fake(*args):
        calls.append(args)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(svc.ScheduledPickupService, method, staticmethod(fake))
    return calls


@pytest.mark.api
def test_schedule_returns_the_stored_row(authed_client, owned, monkeypatch):
    calls = _stub(monkeypatch, "schedule", OK)
    r = authed_client.post("/v1/internal/teams/7/pickups", json=BODY)
    assert r.status_code == 200
    team, league_info, req = calls[0]
    assert team.team_id == 7 and team.user_id == 42 and league_info == ESPN and req == SchedulePickupReq(**BODY)
    d = r.json()["data"]
    assert (d["id"], d["status"], d["scoring_period_id"], d["nba_date"]) == (5, "pending", 3, "2026-10-22")
    assert d["add"] == {"player_id": 6450, "name": "Kawhi Leonard", "team": "LAC", "nba_player_id": 202695}
    assert d["drop"]["name"] == "Anthony Edwards" and d["seated_slot"] is None


@pytest.mark.api
def test_unowned_team_is_404(authed_client, owned, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: None)
    calls = _stub(monkeypatch, "schedule", OK)
    assert authed_client.post("/v1/internal/teams/8/pickups", json=BODY).status_code == 404
    assert authed_client.get("/v1/internal/teams/8/pickups").status_code == 404
    assert authed_client.delete("/v1/internal/teams/8/pickups/5").status_code == 404
    assert calls == []


@pytest.mark.api
@pytest.mark.parametrize("body", [
    {"add_player_id": 0, "scoring_period_id": 3},
    {"scoring_period_id": 3},
    {"add_player_id": 6450, "scoring_period_id": 0},
])
def test_a_bad_body_is_fastapis_422(authed_client, owned, monkeypatch, body):
    calls = _stub(monkeypatch, "schedule", OK)
    assert authed_client.post("/v1/internal/teams/7/pickups", json=body).status_code == 422
    assert calls == []


@pytest.mark.api
def test_invalid_carries_its_reason(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "schedule", svc.ScheduledPickupInvalid(
        message="Day 2 is not after today", data={"reason": "not_future", "player_id": None}))
    r = authed_client.post("/v1/internal/teams/7/pickups", json=BODY)
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "SCHEDULED_PICKUP_INVALID" and body["data"]["reason"] == "not_future"


@pytest.mark.api
def test_out_of_range_is_400(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "schedule", BadRequestError("SCORING_PERIOD_OUT_OF_RANGE", "ESPN day 200 is after the season"))
    r = authed_client.post("/v1/internal/teams/7/pickups", json=BODY)
    assert r.status_code == 400 and r.json()["error_code"] == "SCORING_PERIOD_OUT_OF_RANGE"


@pytest.mark.api
def test_duplicate_is_409(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "schedule", svc.ScheduledPickupDuplicate())
    r = authed_client.post("/v1/internal/teams/7/pickups", json=BODY)
    assert r.status_code == 409 and r.json()["error_code"] == "SCHEDULED_PICKUP_DUPLICATE"


@pytest.mark.api
def test_writes_disabled_and_blocked_keep_the_editors_statuses(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "schedule", editor.RosterWriteDisabled())
    assert authed_client.post("/v1/internal/teams/7/pickups", json=BODY).status_code == 403
    _stub(monkeypatch, "schedule", editor.RosterWriteBlocked(data={"reason": "no_credentials"}))
    r = authed_client.post("/v1/internal/teams/7/pickups", json=BODY)
    assert r.status_code == 409 and r.json()["data"]["reason"] == "no_credentials"


@pytest.mark.api
def test_list_returns_pending_and_recent(authed_client, owned, monkeypatch):
    calls = _stub(monkeypatch, "list_for_team", LISTED)
    r = authed_client.get("/v1/internal/teams/7/pickups")
    assert r.status_code == 200 and calls[0][0].team_id == 7
    d = r.json()["data"]
    assert [p["id"] for p in d["pending"]] == [5] and d["recent"] == []


@pytest.mark.api
def test_cancel_returns_the_cancelled_row(authed_client, owned, monkeypatch):
    cancelled = OK.model_copy(update={"data": PICKUP.model_copy(update={"status": "cancelled"})})
    calls = _stub(monkeypatch, "cancel", cancelled)
    r = authed_client.delete("/v1/internal/teams/7/pickups/5")
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    assert calls[0][0].team_id == 7 and calls[0][1] == 5


@pytest.mark.api
def test_cancel_of_a_settled_row_is_409_and_of_an_unknown_one_404(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "cancel", svc.ScheduledPickupNotPending(data={"status": "executed"}))
    r = authed_client.delete("/v1/internal/teams/7/pickups/5")
    assert r.status_code == 409 and r.json()["error_code"] == "SCHEDULED_PICKUP_NOT_PENDING"
    _stub(monkeypatch, "cancel", NotFoundError("SCHEDULED_PICKUP_NOT_FOUND", "Scheduled pickup not found"))
    assert authed_client.delete("/v1/internal/teams/7/pickups/6").status_code == 404


@pytest.mark.api
def test_cancel_of_a_pickup_being_attempted_is_409_in_progress(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "cancel", svc.ScheduledPickupInProgress(data={"status": "pending"}))
    r = authed_client.delete("/v1/internal/teams/7/pickups/5")
    assert r.status_code == 409
    body = r.json()
    assert body["error_code"] == "SCHEDULED_PICKUP_IN_PROGRESS" and body["data"]["status"] == "pending"
