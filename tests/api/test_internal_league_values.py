"""
API tests for the value-over-a-window endpoint on the teams router.

GET /v1/internal/teams/{team_id}/league/values
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import LeagueInfo

LEAGUE_INFO = '{"provider": "espn", "league_id": 1, "team_name": "T", "year": 2026}'


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


def _values():
    return {
        "league_name": "Test", "format": "points", "today": "2026-11-23", "projection_as_of": "2026-11-23",
        "current_matchup": 6, "regular_season_matchups": 19, "final_matchup": 22, "season_end": "2027-03-28",
        "window": {"key": "playoffs", "first_matchup": 20, "last_matchup": 22, "weeks": [20, 21, 22, 23],
                   "start": "2027-03-01", "end": "2027-03-28", "days": 28, "playoffs": True,
                   "schedule_incomplete": False, "incomplete_weeks": []},
        "players": [{"name": "Star", "player_id": 1, "espn_id": 11, "nba_team": "DEN", "positions": ["C"],
                     "league_team_id": 3, "team_name": "Mine", "mine": True, "slot": "C", "availability": None,
                     "injury_status": None, "per_game": 55.0, "team_games": 15, "games": 14.2, "value": 781.0,
                     "started": 781.0, "window_rank": 1, "season_games": 60.1, "season_value": 3305.5,
                     "season_rank": 1, "projected": True}],
        "free_agents_read": 150, "ranked_players": 280,
        "tiers": [{"tier": 1, "you": 781.0, "rank": 1, "average": 640.0, "median": 630.0, "best": 781.0,
                   "worst": 520.0, "teams": 10}],
        "notes": [],
    }


@pytest.mark.api
def test_values_answer_for_an_owned_team_over_the_window_asked(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))
    seen = {}

    async def fake(league_info, **kwargs):
        seen.update(kwargs, league_id=league_info.league_id)
        return _values()

    monkeypatch.setattr(teams_routes, "league_values", fake)
    res = authed_client.get("/v1/internal/teams/7/league/values?window=playoffs&free_agents=25")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert seen == {"window": "playoffs", "first_matchup": None, "last_matchup": None, "free_agents": 25,
                    "league_id": 1}
    assert body["data"]["players"][0]["started"] == 781.0 and body["data"]["tiers"][0]["average"] == 640.0

    res = authed_client.get("/v1/internal/teams/7/league/values?first_matchup=8&last_matchup=10")
    assert res.status_code == 200
    assert (seen["window"], seen["first_matchup"], seen["last_matchup"], seen["free_agents"]) == (None, 8, 10, 50)


@pytest.mark.api
@pytest.mark.parametrize("query", ["window=someday", "first_matchup=0", "free_agents=500"])
def test_a_window_the_route_can_t_read_is_refused_before_any_read(authed_client, as_user_42, monkeypatch, query):
    from api.v1.internal import teams as teams_routes
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))

    async def never(league_info, **kwargs):
        raise AssertionError("no read for a bad query")

    monkeypatch.setattr(teams_routes, "league_values", never)
    assert authed_client.get(f"/v1/internal/teams/7/league/values?{query}").status_code == 422


@pytest.mark.api
def test_a_window_that_is_over_answers_400_with_its_code(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    from core.errors import BadRequestError
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))

    async def refuse(league_info, **kwargs):
        raise BadRequestError("LEAGUE_VALUES_WINDOW_OVER", "That window is already over")

    monkeypatch.setattr(teams_routes, "league_values", refuse)
    res = authed_client.get("/v1/internal/teams/7/league/values?last_matchup=3")
    assert res.status_code == 400 and res.json()["error_code"] == "LEAGUE_VALUES_WINDOW_OVER"
