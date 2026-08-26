"""
Real HTTP statuses on the top endpoints.

Services either raise `core.errors` AppErrors (rendered by the global handlers)
or return envelopes that `core.responses.respond` maps: SUCCESS is always 200 —
including empty states with `data: null` / `[]` and a message — and only
failures get 4xx/5xx, with the status pinned by `error_code` first, then by the
envelope `status`.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.responses import JSONResponse

from core.errors import BadRequestError, NotFoundError, ProviderAuthError, ProviderError, ProviderTimeout
from core.responses import http_status_for, respond
from schemas.common import ApiStatus, BaseResponse
from schemas.lineup import GenerateLineupResp
from schemas.matchup import MatchupResp
from schemas.rankings import RankingsResp
from schemas.streamer import StreamerResp
from schemas.team import TeamAddResp, TeamGetResp

LEAGUE_INFO = {"provider": "espn", "league_id": 1, "team_name": "T", "year": 2026, "espn_s2": "x", "swid": "y"}


@pytest.fixture
def owned(monkeypatch):
    """Authenticated user 42 who owns team 7 (no database)."""
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


def _stub(monkeypatch, target, name, outcome):
    """Replace `target.name` with a coroutine returning `outcome` (or raising it when it is an exception)."""
    async def fake(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(target, name, staticmethod(fake))


# ---- respond() -----------------------------------------------------------------------


@pytest.mark.api
@pytest.mark.parametrize("status, error_code, expected", [
    (ApiStatus.SUCCESS, None, 200),
    (ApiStatus.BAD_REQUEST, None, 400),
    (ApiStatus.AUTHENTICATION_ERROR, None, 401),
    (ApiStatus.AUTHORIZATION_ERROR, None, 403),
    (ApiStatus.NOT_FOUND, None, 404),
    (ApiStatus.CONFLICT, None, 409),
    (ApiStatus.VALIDATION_ERROR, None, 422),
    (ApiStatus.RATE_LIMITED, None, 429),
    (ApiStatus.SERVER_ERROR, None, 500),
    (ApiStatus.ERROR, None, 500),
    (ApiStatus.ERROR, "LINEUP_SERVICE_UNAVAILABLE", 503),
    (ApiStatus.ERROR, "LINEUP_SERVICE_REJECTED", 422),
    (ApiStatus.ERROR, "PROVIDER_UNAVAILABLE", 502),
    (ApiStatus.ERROR, "PROVIDER_BAD_RESPONSE", 502),
    (ApiStatus.ERROR, "PROVIDER_TIMEOUT", 504),
    (ApiStatus.ERROR, "PROVIDER_AUTH_EXPIRED", 403),
    (ApiStatus.ERROR, "LEAGUE_NOT_FOUND", 400),
    (ApiStatus.ERROR, "LEAGUE_VALIDATION_FAILED", 400),
    (ApiStatus.ERROR, "TEAM_NAME_NOT_IN_LEAGUE", 400),
    (ApiStatus.ERROR, "TEAM_NOT_FOUND", 404),
    (ApiStatus.ERROR, "LINEUP_ALREADY_EXISTS", 409),
    (ApiStatus.SUCCESS, "LINEUP_SERVICE_UNAVAILABLE", 200),      # SUCCESS is 200 whatever the code says
])
def test_envelope_status_map(status, error_code, expected):
    resp = BaseResponse(status=status, message="m", data=None, error_code=error_code)
    assert http_status_for(resp) == expected


@pytest.mark.api
def test_respond_returns_success_envelopes_untouched_and_wraps_failures():
    ok = TeamGetResp(status=ApiStatus.SUCCESS, message="No teams yet", data=[])
    assert respond(ok) is ok                                       # the route's response_model still applies

    failed = TeamAddResp(status=ApiStatus.BAD_REQUEST, message="Team 'X' not in league", team_id=None, already_exists=False)
    out = respond(failed)
    assert isinstance(out, JSONResponse) and out.status_code == 400
    assert out.headers["X-Error-Code"] == "BAD_REQUEST"
    body = json.loads(out.body)
    assert body["error_code"] == "BAD_REQUEST" and body["message"] == "Team 'X' not in league"
    assert body["already_exists"] is False and "team_id" in body   # the envelope keeps its own fields


# ---- endpoints -----------------------------------------------------------------------


@pytest.mark.api
def test_empty_states_stay_200_success(authed_client, client, owned, monkeypatch):
    from services import matchup_service, rankings_service, streamer_service, team_service
    _stub(monkeypatch, team_service.TeamService, "get_teams",
          TeamGetResp(status=ApiStatus.SUCCESS, message="Teams fetched successfully", data=[]))
    _stub(monkeypatch, matchup_service.MatchupService, "get_matchup_by_team_id",
          MatchupResp(status=ApiStatus.SUCCESS, message="No current matchup found (possibly bye week)", data=None))
    _stub(monkeypatch, rankings_service.RankingsService, "get_rankings",
          RankingsResp(status=ApiStatus.SUCCESS, message="No 2026-27 season data yet — rankings start after opening night", data=[]))
    _stub(monkeypatch, streamer_service.StreamerService, "find_streamers",
          StreamerResp(status=ApiStatus.SUCCESS, message="No active matchup for today", data=None))

    res = authed_client.get("/v1/internal/teams/")
    assert res.status_code == 200 and res.json()["data"] == []

    res = authed_client.get("/v1/internal/matchups/current/7")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and body["data"] is None and "bye week" in body["message"]
    assert "X-Error-Code" not in res.headers

    res = client.get("/v1/rankings/")
    assert res.status_code == 200 and res.json()["data"] == [] and "opening night" in res.json()["message"]

    res = authed_client.post("/v1/internal/streamers/find", json={"league_info": LEAGUE_INFO})
    assert res.status_code == 200 and res.json()["data"] is None and res.json()["status"] == "success"


@pytest.mark.api
def test_provider_auth_expired_is_403_naming_the_provider(authed_client, owned, monkeypatch):
    from services import matchup_service
    _stub(monkeypatch, matchup_service.MatchupService, "get_matchup_by_team_id", ProviderAuthError("yahoo"))

    res = authed_client.get("/v1/internal/matchups/current/7", headers={"X-Correlation-ID": "t-yahoo"})

    assert res.status_code == 403
    body = res.json()
    assert body["status"] == "authorization_error" and body["error_code"] == "PROVIDER_AUTH_EXPIRED"
    assert body["data"]["provider"] == "yahoo" and body["data"]["correlation_id"] == "t-yahoo"
    assert res.headers["X-Error-Code"] == "PROVIDER_AUTH_EXPIRED" and res.headers["X-Correlation-ID"] == "t-yahoo"


@pytest.mark.api
def test_provider_outages_are_502_and_504(authed_client, owned, monkeypatch):
    from services import matchup_service
    _stub(monkeypatch, matchup_service.MatchupService, "get_live_matchup_by_team_id", ProviderTimeout("espn"))
    res = authed_client.get("/v1/internal/matchups/live/7")
    assert res.status_code == 504 and res.json()["error_code"] == "PROVIDER_TIMEOUT"

    _stub(monkeypatch, matchup_service.MatchupService, "get_live_matchup_by_team_id",
          ProviderError("espn", error_code="PROVIDER_BAD_RESPONSE"))
    res = authed_client.get("/v1/internal/matchups/live/7")
    assert res.status_code == 502
    assert res.json()["error_code"] == "PROVIDER_BAD_RESPONSE" and res.json()["status"] == "server_error"

    _stub(monkeypatch, matchup_service.MatchupService, "get_score_history",
          NotFoundError("SCORE_HISTORY_NOT_FOUND", "No score history found for this team"))
    res = authed_client.get("/v1/internal/matchups/history/7")
    assert res.status_code == 404 and res.json()["error_code"] == "SCORE_HISTORY_NOT_FOUND"


@pytest.mark.api
def test_team_routes_404_and_400(authed_client, owned, monkeypatch):
    from services import team_service
    _stub(monkeypatch, team_service.TeamService, "remove_team", NotFoundError("TEAM_NOT_FOUND", "Team not found"))
    res = authed_client.delete("/v1/internal/teams/remove?team_id=99")
    assert res.status_code == 404 and res.json()["error_code"] == "TEAM_NOT_FOUND"

    _stub(monkeypatch, team_service.TeamService, "add_team",
          BadRequestError("TEAM_NAME_NOT_IN_LEAGUE", "Team 'T' not found in league 1"))
    res = authed_client.post("/v1/internal/teams/add", json={"league_info": LEAGUE_INFO})
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "bad_request" and body["error_code"] == "TEAM_NAME_NOT_IN_LEAGUE" and "'T'" in body["message"]

    _stub(monkeypatch, team_service.TeamService, "update_team", NotFoundError("TEAM_NOT_FOUND", "Team not found"))
    res = authed_client.put("/v1/internal/teams/update", json={"team_id": 99, "league_info": LEAGUE_INFO})
    assert res.status_code == 404

    _stub(monkeypatch, team_service.TeamService, "add_team",
          TeamAddResp(status=ApiStatus.BAD_REQUEST, message="Invalid league information",
                      error_code="LEAGUE_VALIDATION_FAILED", team_id=None, already_exists=False))
    res = authed_client.post("/v1/internal/teams/add", json={"league_info": LEAGUE_INFO})
    assert res.status_code == 400 and res.json()["already_exists"] is False


@pytest.mark.api
def test_lineup_service_codes_pin_the_status(authed_client, owned, monkeypatch):
    from api.v1.internal import lineups
    from services import lineup_service
    monkeypatch.setattr(lineups, "get_matchup_by_number", lambda n: {"matchup_number": n})
    body = {"team_id": 7, "streaming_slots": 2, "week": 3, "avg_mode": "season"}

    _stub(monkeypatch, lineup_service.LineupService, "generate_lineup",
          GenerateLineupResp(status=ApiStatus.ERROR, message="Lineup service unavailable, try again in a minute",
                             data=None, error_code="LINEUP_SERVICE_UNAVAILABLE"))
    res = authed_client.post("/v1/internal/lineups/generate", json=body)
    assert res.status_code == 503
    assert res.json()["error_code"] == "LINEUP_SERVICE_UNAVAILABLE" and res.headers["X-Error-Code"] == "LINEUP_SERVICE_UNAVAILABLE"
    assert "try again" in res.json()["message"]

    _stub(monkeypatch, lineup_service.LineupService, "generate_lineup",
          GenerateLineupResp(status=ApiStatus.ERROR, message="unknown week 99 (calendar has 24 weeks)",
                             data=None, error_code="LINEUP_SERVICE_REJECTED"))
    res = authed_client.post("/v1/internal/lineups/generate", json=body)
    assert res.status_code == 422 and res.json()["error_code"] == "LINEUP_SERVICE_REJECTED"

    _stub(monkeypatch, lineup_service.LineupService, "generate_lineup", NotFoundError("TEAM_NOT_FOUND", "Team not found"))
    assert authed_client.post("/v1/internal/lineups/generate", json=body).status_code == 404


@pytest.mark.api
def test_public_player_and_rankings_statuses(client, monkeypatch):
    from services import player_service, rankings_service
    _stub(monkeypatch, player_service.PlayerService, "get_player_stats", NotFoundError("PLAYER_NOT_FOUND", "Player not found"))
    res = client.get("/v1/players/0/stats")
    assert res.status_code == 404
    body = res.json()
    assert body["status"] == "not_found" and body["error_code"] == "PLAYER_NOT_FOUND" and body["data"]["correlation_id"]

    _stub(monkeypatch, rankings_service.RankingsService, "get_rankings",
          RankingsResp(status=ApiStatus.BAD_REQUEST, message="Invalid window 5", data=[]))
    res = client.get("/v1/rankings/")
    assert res.status_code == 400
    assert res.json()["status"] == "bad_request" and res.json()["error_code"] == "BAD_REQUEST"
    assert res.headers["X-Error-Code"] == "BAD_REQUEST"

    assert client.get("/v1/rankings/?window=5").status_code == 422          # FastAPI validation, before the service


@pytest.mark.api
def test_streamer_bad_request_is_400(authed_client, monkeypatch):
    from services import streamer_service
    _stub(monkeypatch, streamer_service.StreamerService, "find_streamers",
          BadRequestError("TARGET_DAY_OUT_OF_RANGE", "Day 9 is out of bounds. Matchup has 7 days (0-6)."))
    res = authed_client.post("/v1/internal/streamers/find",
                             json={"league_info": LEAGUE_INFO, "mode": "daily", "target_day": 9})
    assert res.status_code == 400 and res.json()["error_code"] == "TARGET_DAY_OUT_OF_RANGE"


@pytest.mark.api
def test_live_scoreboard_failure_is_a_502_provider_error(client, monkeypatch):
    from api.v1.public import live

    class Broken:
        def get_scoreboard_games(self, game_date):
            raise RuntimeError("cdn exploded: secret detail")

    monkeypatch.setattr(live, "NBAApiExtractor", Broken)
    res = client.get("/v1/live/scoreboard")
    assert res.status_code == 502
    body = res.json()
    assert body["error_code"] == "PROVIDER_UNAVAILABLE" and body["data"]["provider"] == "nba"
    assert "secret detail" not in res.text
