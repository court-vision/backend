"""
API tests for the teams overview on the teams router.

GET /v1/internal/teams/overview

The reads behind it are services.team_overview_service's (tests/unit/test_team_overview_service.py);
here the route is checked for its envelope, the user it asks for, and the shape the
frontend generates its types from.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from schemas.common import FantasyProvider
from schemas.daily_actions import DailyAction, DailyActionPlayer
from schemas.team_overview import (
    OverviewError,
    OverviewItem,
    OverviewMatchup,
    OverviewSeat,
    OverviewTonight,
    TeamOverview,
    TeamsOverviewData,
)

NOW = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
TIP = datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def as_user_42(monkeypatch):
    from services import user_sync_service
    user = MagicMock()
    user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: user))


def _overview() -> TeamsOverviewData:
    start = DailyAction(id="start:11", kind="start", title="Start Filler",
                        player=DailyActionPlayer(player_id=11, name="Filler", team="DEN"), game_time_et="19:30")
    needs = TeamOverview(
        team_id=23, team_name="Lvl. 3 Goblins", league_name="League One", provider=FantasyProvider.ESPN,
        scoring_format="points", state="needs_you",
        items=[OverviewItem(standing="needs_you", deadline_et="19:30", deadline_at=TIP, action=start)],
        tonight=OverviewTonight(first_tip_et="19:00", seats=[OverviewSeat(slot_id=0, slot="PG", mark="empty")],
                                counts={"empty": 1}),
        matchup=OverviewMatchup(matchup_period=5, start="2026-11-16", end="2026-11-22", opponent_name="Rival",
                                your_score=412.5, opponent_score=399.0, your_projected=980.0,
                                opponent_projected=955.0, win_chance=0.64),
        nba_date="2026-11-19", roster_version="v1", scoring_period_id=31, can_write=True, season_state="pending",
    )
    broken = TeamOverview(team_id=21, team_name="James's Finest Team", provider=FantasyProvider.ESPN, state="cant_read",
                          error=OverviewError(code="PROVIDER_AUTH_EXPIRED", message="Reconnect ESPN"))
    counts = {"needs_you": 1, "your_call": 0, "set": 0, "autopilot": 0, "cant_read": 1}
    return TeamsOverviewData(as_of=NOW, counts=counts, next_deadline_at=TIP, teams=[needs, broken])


@pytest.mark.api
def test_the_overview_answers_every_team_for_the_signed_in_user(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes
    seen = {}

    async def fake(user_id):
        seen["user_id"] = user_id
        return _overview()

    monkeypatch.setattr(teams_routes.team_overview_service, "overview", fake)
    res = authed_client.get("/v1/internal/teams/overview")
    assert res.status_code == 200 and seen == {"user_id": 42}
    body = res.json()
    assert body["status"] == "success" and body["message"] == "2 team(s)"
    data = body["data"]
    assert data["counts"]["needs_you"] == 1 and data["next_deadline_at"].startswith("2026-11-20T00:30")
    needs, broken = data["teams"]
    assert needs["state"] == "needs_you" and needs["items"][0]["action"]["id"] == "start:11"
    assert needs["items"][0]["deadline_et"] == "19:30" and needs["matchup"]["win_chance"] == 0.64
    assert needs["tonight"]["seats"][0] == {
        "slot_id": 0, "slot": "PG", "mark": "empty", "player_id": None, "nba_player_id": None, "name": None,
        "team": None, "injury_status": None, "opponent": None, "game_time_et": None, "points": None}
    assert needs["season"] is None and needs["season_state"] == "pending" and needs["week"] == []
    # one team that can't be read is its own line, and the answer is still a 200
    assert broken["state"] == "cant_read" and broken["error"]["code"] == "PROVIDER_AUTH_EXPIRED"
    assert broken["items"] == [] and broken["tonight"] is None and broken["can_write"] is False


@pytest.mark.api
def test_an_account_without_teams_is_an_empty_success(authed_client, as_user_42, monkeypatch):
    from api.v1.internal import teams as teams_routes

    async def fake(user_id):
        return TeamsOverviewData(as_of=NOW, counts={}, teams=[])

    monkeypatch.setattr(teams_routes.team_overview_service, "overview", fake)
    res = authed_client.get("/v1/internal/teams/overview")
    assert res.status_code == 200
    assert res.json()["data"]["teams"] == [] and res.json()["message"] == "0 team(s)"


@pytest.mark.api
def test_the_overview_declares_its_response_schema():
    """The frontend generates its types from the schema: the route must name a concrete model."""
    from main import app
    schema = app.openapi()
    op = schema["paths"]["/v1/internal/teams/overview"]["get"]
    assert op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/TeamsOverviewResp")
    team = schema["components"]["schemas"]["TeamOverview"]
    assert team["properties"]["state"]["enum"] == ["needs_you", "your_call", "set", "autopilot", "cant_read"]
