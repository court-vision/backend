"""
/v1/internal/teams/{id}/stream-lanes over the test app: ownership via the usual
`ensure_team_owned` stub, the credential loader stubbed at the route, and the service
replaced per test so the routes' plumbing and every documented status are exercised —
200, 404 STREAM_LANE_NOT_FOUND, 403 ROSTER_WRITE_DISABLED, 409 STREAM_LANE_EXISTS /
STREAM_LANE_ENDED / STREAM_LANE_BUSY, 422 STREAM_LANE_INVALID with `data.reason`, and
FastAPI's own 422.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import stream_lanes as routes
from core.errors import NotFoundError
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.stream_lane import (
    LanePlayer,
    LaneProjection,
    LaneProjectionDay,
    StreamLane,
    StreamLaneListData,
    StreamLaneListResp,
    StreamLanePlan,
    StreamLanePlanReq,
    StreamLanePlanResp,
    StreamLaneResp,
    StreamLaneStartReq,
    StreamLaneUpdateReq,
)
from services import lineup_editor_service as editor
from services import stream_lane_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")
AT = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)
EDWARDS = LanePlayer(player_id=4594268, name="Anthony Edwards", team="MIN")
KAWHI = LanePlayer(player_id=6450, name="Kawhi Leonard", team="LAC", nba_player_id=202695)
PROJECTION = LaneProjection(as_of=AT, value=40.0, hold_value=30.0, gain=10.0, adds=1, adds_left=3, rehold_after=2, days=[
    LaneProjectionDay(scoring_period_id=28, nba_date=date(2026, 11, 16), player=EDWARDS, value=15.0),
    LaneProjectionDay(scoring_period_id=29, nba_date=date(2026, 11, 17), player=KAWHI, value=25.0, switch=True),
])
LANE = StreamLane(id=3, team_id=7, status="active", notify="email", holder=EDWARDS, original=EDWARDS, start_period=28,
                  end_period=34, end_date=date(2026, 11, 22), next_period=28, next_decision_at=AT, projection=PROJECTION,
                  created_at=AT)
LANE_RESP = StreamLaneResp(status=ApiStatus.SUCCESS, message="Streaming", data=LANE)


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
    monkeypatch.setattr(svc.StreamLaneService, method, staticmethod(fake))
    return calls


@pytest.mark.api
def test_plan_returns_the_projection(authed_client, owned, monkeypatch):
    plan = StreamLanePlan(holder=EDWARDS, start_period=28, end_period=34, end_date=date(2026, 11, 22),
                          first_decision_at=AT, projection=PROJECTION)
    calls = _stub(monkeypatch, "plan", StreamLanePlanResp(status=ApiStatus.SUCCESS, message="Projected", data=plan))
    r = authed_client.post("/v1/internal/teams/7/stream-lanes/plan", json={"player_id": 4594268})
    assert r.status_code == 200
    team, league_info, req = calls[0]
    assert team.team_id == 7 and league_info == ESPN and req == StreamLanePlanReq(player_id=4594268)
    days = r.json()["data"]["projection"]["days"]
    assert [(d["player"]["name"], d["switch"]) for d in days] == [("Anthony Edwards", False), ("Kawhi Leonard", True)]


@pytest.mark.api
def test_start_passes_the_notify_mode(authed_client, owned, monkeypatch):
    calls = _stub(monkeypatch, "start", LANE_RESP)
    r = authed_client.post("/v1/internal/teams/7/stream-lanes", json={"player_id": 4594268, "notify": "desk"})
    assert r.status_code == 200 and r.json()["data"]["id"] == 3
    assert calls[0][2] == StreamLaneStartReq(player_id=4594268, notify="desk")


@pytest.mark.api
def test_list_update_and_stop(authed_client, owned, monkeypatch):
    _stub(monkeypatch, "list_for_team", StreamLaneListResp(status=ApiStatus.SUCCESS, message="1 open lane(s)",
                                                           data=StreamLaneListData(open=[LANE], recent=[])))
    r = authed_client.get("/v1/internal/teams/7/stream-lanes")
    assert r.status_code == 200 and r.json()["data"]["open"][0]["holder"]["name"] == "Anthony Edwards"
    calls = _stub(monkeypatch, "update", LANE_RESP)
    r = authed_client.patch("/v1/internal/teams/7/stream-lanes/3", json={"status": "paused"})
    assert r.status_code == 200
    _, league_info, lane_id, req = calls[0]
    assert (league_info, lane_id, req) == (ESPN, 3, StreamLaneUpdateReq(status="paused"))
    calls = _stub(monkeypatch, "stop", LANE_RESP)
    assert authed_client.delete("/v1/internal/teams/7/stream-lanes/3").status_code == 200 and calls[0][1] == 3


@pytest.mark.api
@pytest.mark.parametrize("method, verb, path, body, error, status, code", [
    ("start", "post", "", {"player_id": 1}, editor.RosterWriteDisabled(), 403, "ROSTER_WRITE_DISABLED"),
    ("start", "post", "", {"player_id": 1}, svc.StreamLaneExists(), 409, "STREAM_LANE_EXISTS"),
    ("plan", "post", "/plan", {"player_id": 1}, svc._invalid("on_ir", "On IR"), 422, "STREAM_LANE_INVALID"),
    ("update", "patch", "/3", {"status": "active"}, svc.StreamLaneEnded(), 409, "STREAM_LANE_ENDED"),
    ("update", "patch", "/3", {"status": "paused"}, svc.StreamLaneBusy(), 409, "STREAM_LANE_BUSY"),
    ("stop", "delete", "/3", None, svc.StreamLaneBusy(), 409, "STREAM_LANE_BUSY"),
    ("stop", "delete", "/9", None, NotFoundError("STREAM_LANE_NOT_FOUND", "Stream lane not found"), 404,
     "STREAM_LANE_NOT_FOUND"),
])
def test_refusals_carry_their_status_and_code(authed_client, owned, monkeypatch, method, verb, path, body, error,
                                              status, code):
    _stub(monkeypatch, method, error)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(authed_client, verb)(f"/v1/internal/teams/7/stream-lanes{path}", **kwargs)
    assert r.status_code == status and r.json()["error_code"] == code
    if code == "STREAM_LANE_INVALID":
        assert r.json()["data"]["reason"] == "on_ir"


@pytest.mark.api
@pytest.mark.parametrize("verb, path, body", [
    ("post", "", {"player_id": 0}),
    ("post", "", {"player_id": 1, "notify": "sms"}),
    ("patch", "/3", {"status": "ended"}),
])
def test_bad_bodies_are_fastapis_422(authed_client, owned, verb, path, body):
    r = getattr(authed_client, verb)(f"/v1/internal/teams/7/stream-lanes{path}", json=body)
    assert r.status_code == 422
