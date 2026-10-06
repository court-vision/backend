"""
/v1/internal/jobs/lineup/evaluate and /jobs/pickups/execute: the pipeline-token
routes. No Clerk user — a missing or wrong bearer is refused, the right one reaches
the (stubbed) service.
"""

import pytest

from core import pipeline_auth
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupEvaluationData, LineupEvaluationResp
from schemas.scheduled_pickup import PickupExecuteData, PickupExecuteResp
from services import lineup_editor_service as svc
from services import scheduled_pickup_service as pickups

BODY = {"team_id": 21, "user_id": 11, "nba_date": "2026-10-20", "apply": False}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(pipeline_auth, "PIPELINE_API_TOKEN", "pipe-token")


@pytest.mark.api
def test_missing_bearer_is_refused(client, token):
    assert client.post("/v1/internal/jobs/lineup/evaluate", json=BODY).status_code in (401, 403)


@pytest.mark.api
def test_wrong_bearer_is_401(client, token):
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.api
def test_right_bearer_reaches_the_service(client, token, monkeypatch):
    seen = []

    async def fake(req):
        seen.append(req)
        return LineupEvaluationResp(status=ApiStatus.SUCCESS, message="Lineup planned",
                                    data=LineupEvaluationData(outcome="planned", team_name="T"))

    monkeypatch.setattr(svc.LineupEditorService, "evaluate", staticmethod(fake))
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer pipe-token"})
    assert r.status_code == 200 and r.json()["data"]["outcome"] == "planned"
    assert seen[0].team_id == 21 and seen[0].apply is False and str(seen[0].nba_date) == "2026-10-20"


@pytest.mark.api
def test_unconfigured_token_is_a_500(client, monkeypatch):
    monkeypatch.setattr(pipeline_auth, "PIPELINE_API_TOKEN", None)
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer x"})
    assert r.status_code == 500


# ---- /v1/internal/jobs/pickups/execute ------------------------------------------

PICKUPS = "/v1/internal/jobs/pickups/execute"


@pytest.mark.api
def test_pickups_missing_or_wrong_bearer_is_refused(client, token):
    assert client.post(PICKUPS, json={}).status_code in (401, 403)
    assert client.post(PICKUPS, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.api
def test_pickups_right_bearer_reaches_the_service_with_defaults(client, token, monkeypatch):
    seen = []

    async def fake(req):
        seen.append(req)
        return PickupExecuteResp(status=ApiStatus.SUCCESS, message="0 pickup(s) attempted",
                                 data=PickupExecuteData(due=0, results=[]))

    monkeypatch.setattr(pickups.ScheduledPickupService, "execute_due", staticmethod(fake))
    r = client.post(PICKUPS, json={}, headers={"Authorization": "Bearer pipe-token"})
    assert r.status_code == 200 and r.json()["data"] == {"due": 0, "results": []}
    assert seen[0].limit == 4 and seen[0].now is None

    r = client.post(PICKUPS, json={"limit": 2, "now": "2026-10-22T00:05:00Z"}, headers={"Authorization": "Bearer pipe-token"})
    assert r.status_code == 200 and seen[1].limit == 2 and seen[1].now.isoformat() == "2026-10-22T00:05:00+00:00"
    assert client.post(PICKUPS, json={"limit": 0}, headers={"Authorization": "Bearer pipe-token"}).status_code == 422


# ---- /v1/internal/jobs/valuation/standard --------------------------------------

VALUATION = "/v1/internal/jobs/valuation/standard"
AUTH = {"Authorization": "Bearer pipe-token"}


def _player(pid, pts, **extra):
    return {"player_id": pid, "games": 70, "team": "DEN",
            "line": {"pts": pts, "reb": 5, "ast": 4, "stl": 1, "blk": 0.5, "tov": 2,
                     "fgm": pts / 2.5, "fga": pts / 1.2, "fg3m": 1.5, "fg3a": 4,
                     "ftm": 3, "fta": 4, "min": 30}, **extra}


@pytest.mark.api
def test_valuation_needs_the_pipeline_token(client, token):
    body = {"players": [_player(1, 20)]}
    assert client.post(VALUATION, json=body).status_code in (401, 403)
    assert client.post(VALUATION, json=body, headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.api
def test_valuation_ranks_the_pool_it_is_sent(client, token):
    """The real valuation, on a pool small enough to read: more points is a
    better points rank, and the answer comes back in the order sent."""
    body = {"players": [_player(3, 12), _player(1, 30), _player(2, 21)]}

    r = client.post(VALUATION, json=body, headers=AUTH)

    assert r.status_code == 200
    data = r.json()["data"]
    assert (data["league_size"], data["rounds"], data["playoff_weight"]) == (12, 13, 2.0)
    assert [p["player_id"] for p in data["players"]] == [3, 1, 2]
    assert [p["points_rank"] for p in data["players"]] == [3, 1, 2]
    assert [p["category_rank"] for p in data["players"]] == [3, 1, 2]
    assert all(p["points_value"] > 0 and p["games"] > 0 for p in data["players"])
    # The season's calendar is on disk, so the default playoff weeks are named.
    assert data["playoff_weeks"] == [20, 21, 22, 23]


@pytest.mark.api
def test_valuation_snaps_the_playoff_weight_and_refuses_one_out_of_range(client, token):
    body = {"players": [_player(1, 30), _player(2, 21)], "playoff_weight": 2.8}
    r = client.post(VALUATION, json=body, headers=AUTH)
    assert r.status_code == 200 and r.json()["data"]["playoff_weight"] == 3.0

    body["playoff_weight"] = 9
    assert client.post(VALUATION, json=body, headers=AUTH).status_code == 422


@pytest.mark.api
def test_valuation_refuses_an_empty_pool_and_impossible_games(client, token):
    assert client.post(VALUATION, json={"players": []}, headers=AUTH).status_code == 422
    assert client.post(VALUATION, json={"players": [_player(1, 30, games=120)]},
                       headers=AUTH).status_code == 422
    # A line with no stats at all is still a line: he is simply worth nothing.
    r = client.post(VALUATION, json={"players": [_player(1, 30), {"player_id": 2, "line": {}}]},
                    headers=AUTH)
    assert r.status_code == 200
    assert [p["points_rank"] for p in r.json()["data"]["players"]] == [1, 2]
