"""
API tests for the streaming line on the teams router.

GET /v1/internal/teams/{team_id}/league/streaming-line
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import LeagueInfo

LEAGUE_INFO = '{"provider": "espn", "league_id": 1, "team_name": "T", "year": 2027}'


@pytest.fixture
def as_user_42(monkeypatch):
    from services import user_sync_service
    user = MagicMock()
    user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: user))


def _own_team(monkeypatch, team):
    from api import deps
    from api.v1.internal import teams as teams_routes
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: team)

    async def hydrate(_team):
        return LeagueInfo.model_validate_json(team.league_info)
    monkeypatch.setattr(teams_routes, "load_owned_league_info", hydrate)


def _line():
    return {
        "league_name": "Test", "today": "2026-11-26", "projection_as_of": "2026-11-26", "current_matchup": 6,
        "key": "this_week", "first_matchup": 6, "last_matchup": 6, "line": 80.0, "line_low": 64.0,
        "matchups": [{"matchup": 6, "start": "2026-11-27", "end": "2026-11-29", "days": 3, "adds": 4,
                      "playoffs": False, "base": 600.0,
                      "lanes": [{"lane": 1, "holder": "Bench", "holder_espn_id": 5, "total": 660.0, "gain": 60.0,
                                 "adds": 2}],
                      "worth_running": 1, "line": 80.0, "line_low": 64.0, "proven": True}],
        "players": [{"name": "Bench", "espn_id": 5, "worth": 20.0, "by_matchup": [20.0], "under_line": True}],
        "free_agents_read": 150, "notes": [],
    }


@pytest.mark.api
def test_the_line_answers_for_an_owned_team_over_the_window_asked(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))
    seen = {}

    async def fake(team_id, league_info, **kwargs):
        seen.update(kwargs, team_id=team_id, league_id=league_info.league_id)
        return _line()

    monkeypatch.setattr(teams_routes, "streaming_line", fake)
    res = authed_client.get("/v1/internal/teams/7/league/streaming-line?window=playoffs")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and body["data"]["matchups"][0]["lanes"][0]["gain"] == 60.0
    assert seen == {"window": "playoffs", "first_matchup": None, "last_matchup": None, "team_id": 7, "league_id": 1}
    assert authed_client.get("/v1/internal/teams/7/league/streaming-line?window=someday").status_code == 422


@pytest.mark.api
def test_a_window_too_long_answers_400_with_its_code(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    from core.errors import BadRequestError
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))

    async def refuse(team_id, league_info, **kwargs):
        raise BadRequestError("LEAGUE_STREAMING_LINE_TOO_LONG", "Too long")

    monkeypatch.setattr(teams_routes, "streaming_line", refuse)
    res = authed_client.get("/v1/internal/teams/7/league/streaming-line?window=rest_of_season")
    assert res.status_code == 400 and res.json()["error_code"] == "LEAGUE_STREAMING_LINE_TOO_LONG"
