"""
API tests for the league settings endpoints on the teams router.

GET  /v1/internal/teams/{team_id}/league
POST /v1/internal/teams/{team_id}/league/sync
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import ApiStatus


def _fake_user(user_id: int = 42) -> MagicMock:
    user = MagicMock()
    user.user_id = user_id
    return user


def _fake_league(**overrides) -> SimpleNamespace:
    base = dict(
        id=3, provider="espn", provider_league_id="993431466", season=2026, name="Test",
        scoring_type="categories", category_win_mode="each_category",
        categories=[{"key": "pts", "label": "PTS", "higher_is_better": True, "is_rate": False},
                    {"key": "tov", "label": "TO", "higher_is_better": False, "is_rate": False}],
        point_weights={}, matchup_periods={"periods": {"1": [1]}}, roster_slots={"PG": 1},
        raw_settings={"_sync": {"unsupported": ["espn:99:?"], "warnings": []}},
        settings_synced_at=datetime(2026, 8, 24, 12, 0, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def as_user_42(monkeypatch):
    from services import user_sync_service
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: _fake_user(42)))


def _own_team(monkeypatch, team):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: team)


@pytest.mark.api
def test_get_league_unsynced_returns_success_with_no_data(authed_client, as_user_42, monkeypatch):
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_id=None, league=None, league_info="{}"))
    res = authed_client.get("/v1/internal/teams/7/league")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and body["data"] is None


@pytest.mark.api
def test_get_league_returns_detail(authed_client, as_user_42, monkeypatch):
    league = _fake_league()
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_id=3, league=league, league_info="{}"))
    res = authed_client.get("/v1/internal/teams/7/league")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["scoring_type"] == "categories" and data["category_win_mode"] == "each_category"
    assert [c["key"] for c in data["categories"]] == ["pts", "tov"]
    assert data["settings_synced"] is True and data["unsupported"] == ["espn:99:?"]
    assert data["roster_slots"] == {"PG": 1} and data["matchup_periods"]["periods"]["1"] == [1]


@pytest.mark.api
def test_sync_league_calls_service_and_returns_summary(authed_client, as_user_42, monkeypatch):
    from services import league_service
    team = SimpleNamespace(team_id=7, user_id_id=42, league_id=None, league=None,
                           league_info='{"provider": "espn", "league_id": 1, "team_name": "T", "year": 2026}')
    _own_team(monkeypatch, team)
    captured = {}

    async def fake_sync(t, league_info, espn_payload=None):
        captured["team_id"] = t.team_id
        captured["league_id"] = league_info.league_id
        return _fake_league(scoring_type="points", point_weights={"pts": 1.0}, categories=[])

    monkeypatch.setattr(league_service.LeagueService, "sync_for_team", staticmethod(fake_sync))
    res = authed_client.post("/v1/internal/teams/7/league/sync")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and captured == {"team_id": 7, "league_id": 1}
    assert body["data"]["scoring_type"] == "points" and body["data"]["point_weights"] == {"pts": 1.0}


@pytest.mark.api
def test_sync_league_reports_unsynced_stub_as_error_status(authed_client, as_user_42, monkeypatch):
    from services import league_service
    team = SimpleNamespace(team_id=7, user_id_id=42, league_id=None, league=None,
                           league_info='{"provider": "espn", "league_id": 1, "team_name": "T", "year": 2026}')
    _own_team(monkeypatch, team)

    async def fake_sync(t, league_info, espn_payload=None):
        return _fake_league(settings_synced_at=None, scoring_type="points", categories=[], raw_settings=None)

    monkeypatch.setattr(league_service.LeagueService, "sync_for_team", staticmethod(fake_sync))
    body = authed_client.post("/v1/internal/teams/7/league/sync").json()
    assert body["status"] == "error" and body["data"]["settings_synced"] is False


@pytest.mark.api
def test_league_routes_404_for_foreign_team(authed_client, as_user_42, monkeypatch):
    _own_team(monkeypatch, None)
    assert authed_client.get("/v1/internal/teams/999/league").status_code == 404
    assert authed_client.post("/v1/internal/teams/999/league/sync").status_code == 404
