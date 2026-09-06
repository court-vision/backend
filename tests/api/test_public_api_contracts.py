"""Public HTTP contracts: errors, identifiers, new snapshots, and real quota keys."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import Request, Security

from core import api_key_auth
from core.rate_limit import get_rate_limit_key, limiter
from schemas.common import ApiStatus, BaseResponse
from schemas.market import ESPNMarketData, ESPNMarketResp, PlayerProjectionResp
from schemas.player import PlayerStatsResp
from services.public_market_service import PublicMarketService

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def isolated_limits():
    limiter.reset()
    yield
    limiter.reset()


@pytest.mark.parametrize("path,module,service,method", [
    ("/v1/teams/BAD/schedule", "team_schedule_service", "TeamScheduleService", "get_team_schedule"),
    ("/v1/teams/BAD/stats", "nba_team_stats_service", "NBATeamStatsService", "get_team_stats"),
    ("/v1/teams/BAD/roster", "nba_team_roster_service", "NBATeamRosterService", "get_team_roster"),
    ("/v1/teams/BAD/live-game", "nba_team_live_game_service", "NBATeamLiveGameService", "get_live_game"),
    ("/v1/games/2026-03-01", "games_service", "GamesService", "get_games_on_date"),
    ("/v1/ownership/trending", "ownership_service", "OwnershipService", "get_trending"),
    ("/v1/playoff/bracket", "playoff_service", "PlayoffService", "get_bracket"),
    ("/v1/analytics/breakout-streamers", "breakout_service", "BreakoutService", "get_breakout_candidates"),
])
@pytest.mark.parametrize("status,code,expected", [
    (ApiStatus.NOT_FOUND, None, 404),
    (ApiStatus.ERROR, None, 500),
    (ApiStatus.ERROR, "PROVIDER_TIMEOUT", 504),
])
def test_public_failures_use_http_statuses(client, app, monkeypatch, path, module, service, method, status, code, expected):
    from importlib import import_module

    app.dependency_overrides[api_key_auth.verify_api_key] = lambda: api_key_auth.APIKeyContext(1, ("analytics",))
    target = getattr(import_module(f"services.{module}"), service)
    monkeypatch.setattr(target, method, AsyncMock(return_value=BaseResponse(status=status, message="failure", error_code=code)))
    response = client.get(path)
    assert response.status_code == expected
    assert response.json()["status"] == status
    assert response.headers["X-Error-Code"]


def test_player_path_accepts_the_same_window_as_query_lookup(client, monkeypatch):
    from services.player_service import PlayerService

    stub = AsyncMock(return_value=PlayerStatsResp(status=ApiStatus.SUCCESS, message="empty", data=None))
    monkeypatch.setattr(PlayerService, "get_player_stats", stub)
    assert client.get("/v1/players/2544/stats?window=l5").status_code == 200
    stub.assert_awaited_once_with(player_id=2544, window="l5")
    assert client.get("/v1/players/2544/stats?window=l0").status_code == 422


@pytest.mark.parametrize("path", [
    "/v1/rankings/espn?season=2026-99", "/v1/rankings/espn?season=garbage",
    "/v1/rankings/espn?as_of=2026-02-30", "/v1/rankings/espn?limit=101",
    "/v1/rankings/espn?offset=-1", "/v1/rankings/espn?sort_by=raw",
    "/v1/players/0/projection", "/v1/players/1/projection?season=2026-99",
    "/v1/players/1/projection?as_of=invalid",
])
def test_market_parameters_are_validated_before_service_calls(client, monkeypatch, path):
    market, projection = AsyncMock(), AsyncMock()
    monkeypatch.setattr(PublicMarketService, "get_market", market)
    monkeypatch.setattr(PublicMarketService, "get_projection", projection)
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    market.assert_not_awaited()
    projection.assert_not_awaited()


def test_market_route_serializes_snapshot_dates_and_passes_filters(client, monkeypatch):
    market = AsyncMock(return_value=ESPNMarketResp(
        status=ApiStatus.SUCCESS, message="snapshot",
        data=ESPNMarketData(season="2026-27", as_of_date=date(2026, 9, 1), total=0, limit=10, offset=2, sort_by="adp"),
    ))
    monkeypatch.setattr(PublicMarketService, "get_market", market)
    response = client.get("/v1/rankings/espn?season=2026-27&as_of=2026-09-03&name=Jokic&sort_by=adp&limit=10&offset=2")
    assert response.status_code == 200
    assert response.json()["data"]["as_of_date"] == "2026-09-01"
    market.assert_awaited_once_with(season="2026-27", as_of=date(2026, 9, 3), name="Jokic", sort_by="adp", limit=10, offset=2)


def test_projection_empty_state_and_filter_forwarding(client, monkeypatch):
    projection = AsyncMock(return_value=PlayerProjectionResp(status=ApiStatus.SUCCESS, message="No projection", data=None))
    monkeypatch.setattr(PublicMarketService, "get_projection", projection)
    response = client.get("/v1/players/2544/projection?season=2026-27&as_of=2026-09-03")
    assert response.status_code == 200 and response.json()["data"] is None
    projection.assert_awaited_once_with(player_id=2544, season="2026-27", as_of=date(2026, 9, 3))


def test_changing_anonymous_api_key_headers_cannot_reset_public_quota(app, client):
    @app.get("/quota")
    @limiter.limit("2/minute")
    async def quota(request: Request):
        return {"key": get_rate_limit_key(request)}

    assert client.get("/quota", headers={"X-API-Key": "cv_one"}).status_code == 200
    assert client.get("/quota", headers={"X-API-Key": "cv_two"}).status_code == 200
    response = client.get("/quota", headers={"X-API-Key": "cv_three"})
    assert response.status_code == 429
    assert 0 < int(response.headers["Retry-After"]) <= 60
    assert response.json()["error_code"] == "RATE_LIMITED"


def test_real_auth_dependency_sets_a_private_distinct_rate_identity(app, client, monkeypatch):
    async def direct(operation, fn, *args):
        return await asyncio.to_thread(fn, *args)

    monkeypatch.setattr(api_key_auth, "run_db", direct)
    monkeypatch.setattr(api_key_auth, "_verify_key", lambda raw: api_key_auth.APIKeyContext(1, ("analytics",)))

    @app.get("/key-quota")
    @limiter.limit("1/minute")
    async def quota(request: Request, key=Security(api_key_auth.verify_api_key)):
        return {"identity": get_rate_limit_key(request)}

    first = client.get("/key-quota", headers={"X-API-Key": "cv_sameprefix_one"})
    second = client.get("/key-quota", headers={"X-API-Key": "cv_sameprefix_two"})
    assert first.status_code == second.status_code == 200
    assert first.json()["identity"] != second.json()["identity"]
    assert first.json()["identity"].startswith("api_key:")
    assert "cv_sameprefix" not in first.text
    assert client.get("/key-quota", headers={"X-API-Key": "cv_sameprefix_one"}).status_code == 429


def test_playoff_bracket_has_public_rate_limit():
    # The route was the only public data endpoint without any quota.
    key = "api.v1.public.playoffs.get_playoff_bracket"
    assert str(limiter._route_limits[key][0].limit) == "100 per 1 minute"
