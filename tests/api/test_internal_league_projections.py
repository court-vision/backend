"""
API tests for the league projections endpoint on the teams router.

GET /v1/internal/teams/{team_id}/league/projections
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


def _benchmark(you):
    return {"you": you, "rank": 2, "median": 1100.0, "low": 1050.0, "high": 1150.0, "line": 1120.0,
            "best": 1250.0, "worst": 980.0, "teams": 10}


def _projection():
    return {
        "league_name": "Test", "format": "points", "today": "2026-11-23", "projection_as_of": "2026-11-23",
        "teams_count": 10, "playoff_teams": 6, "seed_tiebreaker": "points_for", "lineup_slots": {"PG": 1, "UT": 3},
        "current_matchup": 6, "regular_season_matchups": 19, "playoff_matchups": [20, 21, 22],
        "playoff_weeks": [21, 22, 23], "incomplete_weeks": [7, 8], "typical_week": 1100.0,
        "simulations": 10000, "in_playoffs": False,
        "benchmarks": {"regular_season": _benchmark(1180.0), "playoffs": _benchmark(1210.0)},
        "teams": [{"league_team_id": 3, "team_name": "Mine", "mine": True, "standing": 4, "wins": 3,
                   "losses": 2, "ties": 0, "points_for": 5600.0, "points_against": 5400.0,
                   "regular_season_week": 1180.0, "regular_season_rank": 2, "playoff_week": 1210.0,
                   "playoff_rank": 2, "projected_wins": 11.5, "playoff_odds": 0.81, "title_odds": 0.17,
                   "seed_odds": [0.2, 0.2, 0.15, 0.1, 0.1, 0.06], "players_projected": 13,
                   "players_unprojected": []}],
        "matchups": [{**_benchmark(1180.0), "matchup": 6, "weeks": [6], "start": "2026-11-23",
                      "end": "2026-11-29", "status": "current", "opponent": "Rival",
                      "opponent_league_team_id": 5, "opponent_score": 1100.0, "win_chance": 0.64}],
        "notes": ["The calendar is missing games in weeks 7 and 8."],
    }


@pytest.mark.api
def test_projections_answer_for_an_owned_team(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))
    seen = {}

    async def fake(league_info, **_):
        seen["league_id"] = league_info.league_id
        return _projection()

    monkeypatch.setattr(teams_routes, "league_projection", fake)
    res = authed_client.get("/v1/internal/teams/7/league/projections")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and seen == {"league_id": 1}
    data = body["data"]
    assert data["benchmarks"]["regular_season"]["line"] == 1120.0
    assert data["teams"][0]["playoff_odds"] == 0.81 and data["matchups"][0]["win_chance"] == 0.64


@pytest.mark.api
def test_a_league_that_cannot_be_projected_answers_400_with_its_code(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    from services.league_projection_service import LeagueProjectionUnavailable
    _own_team(monkeypatch, SimpleNamespace(team_id=7, user_id_id=42, league_info=LEAGUE_INFO))

    async def refuse(league_info, **_):
        raise LeagueProjectionUnavailable("LEAGUE_PROJECTION_POINTS_ONLY", "points leagues only")

    monkeypatch.setattr(teams_routes, "league_projection", refuse)
    res = authed_client.get("/v1/internal/teams/7/league/projections")
    assert res.status_code == 400
    assert res.json()["error_code"] == "LEAGUE_PROJECTION_POINTS_ONLY"
