"""
Ownership enforcement for team- and lineup-scoped internal routes.

A caller-supplied team_id / lineup_id is only honored when the resource
belongs to the authenticated user; otherwise the route returns 404 before
any service is invoked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import ApiStatus
from schemas.lineup import DeleteLineupResp, SaveLineupResp
from schemas.matchup import SeasonSummaryResp
from schemas.team_insights import TeamInsightsResp


def _fake_user(user_id: int = 42) -> MagicMock:
    user = MagicMock()
    user.user_id = user_id
    return user


OWNED_TEAM = SimpleNamespace(team_id=7, user_id_id=42, league_info="{}")
OWNED_LINEUP = SimpleNamespace(lineup_id=5)

TEAM_SCOPED_PATHS = [
    "/v1/internal/teams/view?team_id=999",
    "/v1/internal/teams/999/insights",
    "/v1/internal/matchups/current/999",
    "/v1/internal/matchups/live/999",
    "/v1/internal/matchups/history/999",
    "/v1/internal/matchups/week/999",
    "/v1/internal/matchups/season-summary/999",
    "/v1/internal/matchups/daily/999?date=2026-01-15",
]

VALID_SAVE_BODY = {
    "team_id": 7,
    "lineup_info": {
        "Lineup": [{"Day": 0}],
        "Improvement": 0,
        "Timestamp": "2026-01-15T00:00:00",
        "Week": 1,
        "StreamingSlots": 0,
    },
}


@pytest.fixture
def as_user_42(monkeypatch):
    from services import user_sync_service

    monkeypatch.setattr(
        user_sync_service.UserSyncService,
        "get_or_create_user",
        staticmethod(lambda clerk_id, email: _fake_user(42)),
    )


def _patch_team_lookup(monkeypatch, result):
    """Stub the owned-team query everywhere it is bound."""
    from api import deps
    from api.v1.internal import lineups

    calls = []

    def fake_lookup(team_id, user_id):
        calls.append((team_id, user_id))
        return result

    monkeypatch.setattr(deps, "ensure_team_owned", fake_lookup)
    monkeypatch.setattr(lineups, "ensure_team_owned", fake_lookup)
    return calls


def _patch_lineup_lookup(monkeypatch, result):
    from api import deps

    monkeypatch.setattr(deps, "_find_owned_lineup", lambda lineup_id, user_id: result)


# ---- Team-scoped routes ----

@pytest.mark.api
@pytest.mark.parametrize("path", TEAM_SCOPED_PATHS)
def test_team_routes_404_when_team_not_owned(authed_client, as_user_42, monkeypatch, path):
    calls = _patch_team_lookup(monkeypatch, None)

    res = authed_client.get(path)

    assert res.status_code == 404
    assert calls == [(999, 42)]


@pytest.mark.api
def test_insights_route_uses_owned_team(authed_client, as_user_42, monkeypatch):
    from services import team_insights_service

    _patch_team_lookup(monkeypatch, OWNED_TEAM)
    captured = {}

    async def fake_insights(team_id):
        captured["team_id"] = team_id
        return TeamInsightsResp(status=ApiStatus.SUCCESS, message="ok", data=None)

    monkeypatch.setattr(team_insights_service.TeamInsightsService, "get_team_insights", staticmethod(fake_insights))

    res = authed_client.get("/v1/internal/teams/7/insights")

    assert res.status_code == 200
    assert captured["team_id"] == 7


@pytest.mark.api
def test_season_summary_route_uses_owned_team(authed_client, as_user_42, monkeypatch):
    from services import matchup_service

    _patch_team_lookup(monkeypatch, OWNED_TEAM)
    captured = {}

    async def fake_summary(team_id):
        captured["team_id"] = team_id
        return SeasonSummaryResp(status=ApiStatus.SUCCESS, message="ok", data=None)

    monkeypatch.setattr(matchup_service.MatchupService, "get_season_summary", staticmethod(fake_summary))

    res = authed_client.get("/v1/internal/matchups/season-summary/7")

    assert res.status_code == 200
    assert captured["team_id"] == 7


# ---- Lineup routes ----

@pytest.mark.api
def test_delete_lineup_404_when_not_owned(authed_client, as_user_42, monkeypatch):
    from services import lineup_service

    _patch_lineup_lookup(monkeypatch, None)

    async def must_not_run(lineup_id):
        raise AssertionError("remove_lineup must not be called for a non-owned lineup")

    monkeypatch.setattr(lineup_service.LineupService, "remove_lineup", staticmethod(must_not_run))

    res = authed_client.delete("/v1/internal/lineups/remove?lineup_id=5")

    assert res.status_code == 404


@pytest.mark.api
def test_delete_lineup_passes_owned_lineup(authed_client, as_user_42, monkeypatch):
    from services import lineup_service

    _patch_lineup_lookup(monkeypatch, OWNED_LINEUP)
    captured = {}

    async def fake_remove(lineup_id):
        captured["lineup_id"] = lineup_id
        return DeleteLineupResp(status=ApiStatus.SUCCESS, message="ok")

    monkeypatch.setattr(lineup_service.LineupService, "remove_lineup", staticmethod(fake_remove))

    res = authed_client.delete("/v1/internal/lineups/remove?lineup_id=5")

    assert res.status_code == 200
    assert captured["lineup_id"] == 5


@pytest.mark.api
def test_save_lineup_404_when_team_not_owned(authed_client, as_user_42, monkeypatch):
    from services import lineup_service

    calls = _patch_team_lookup(monkeypatch, None)

    async def must_not_run(user_id, team_id, lineup_info):
        raise AssertionError("save_lineup must not be called for a non-owned team")

    monkeypatch.setattr(lineup_service.LineupService, "save_lineup", staticmethod(must_not_run))

    res = authed_client.put("/v1/internal/lineups/save", json=VALID_SAVE_BODY)

    assert res.status_code == 404
    assert calls == [(7, 42)]


@pytest.mark.api
def test_save_lineup_proceeds_for_owned_team(authed_client, as_user_42, monkeypatch):
    from services import lineup_service

    _patch_team_lookup(monkeypatch, OWNED_TEAM)
    captured = {}

    async def fake_save(user_id, team_id, lineup_info):
        captured.update(user_id=user_id, team_id=team_id)
        return SaveLineupResp(status=ApiStatus.SUCCESS, message="ok")

    monkeypatch.setattr(lineup_service.LineupService, "save_lineup", staticmethod(fake_save))

    res = authed_client.put("/v1/internal/lineups/save", json=VALID_SAVE_BODY)

    assert res.status_code == 200
    assert captured == {"user_id": 42, "team_id": 7}
