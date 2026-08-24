"""Matchup endpoints pass through category-league fields and keep scalar fields numeric."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import ApiStatus
from schemas.matchup import (
    CategoryComparison, CategoryScoreItem, CategoryTeamScore, MatchupData, MatchupResp, MatchupTeamResp,
)


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


def _team(name, score, cats=None):
    return MatchupTeamResp(team_name=name, team_id=1, current_score=score, projected_score=score + 1, roster=[], categories=cats)


@pytest.mark.api
def test_category_matchup_response_shape(authed_client, owned, monkeypatch):
    from services import matchup_service
    cmp = CategoryComparison(items=[CategoryScoreItem(key="pts", label="PTS", you=500, opp=480, winner="you",
                                                       higher_is_better=True, is_rate=False)], wins=1, losses=0, ties=0)
    data = MatchupData(
        matchup_period=4, matchup_period_start="2027-01-18", matchup_period_end="2027-01-24",
        your_team=_team("You", 5.0, CategoryTeamScore(totals={"pts": 500}, wins=5, losses=3, ties=1)),
        opponent_team=_team("Opp", 3.0, CategoryTeamScore(totals={"pts": 480}, wins=3, losses=5, ties=1)),
        projected_winner="You", projected_margin=2.0, scoring_format="categories", settings_synced=True,
        category_comparison=cmp,
    )

    async def fake(user_id, team_id, avg_window):
        return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=data)

    monkeypatch.setattr(matchup_service.MatchupService, "get_matchup_by_team_id", staticmethod(fake))
    body = authed_client.get("/v1/internal/matchups/current/7").json()
    d = body["data"]
    assert d["scoring_format"] == "categories" and d["settings_synced"] is True
    assert d["your_team"]["current_score"] == 5.0 and isinstance(d["projected_margin"], float)
    assert d["your_team"]["categories"]["wins"] == 5 and d["category_comparison"]["items"][0]["winner"] == "you"


@pytest.mark.api
def test_points_matchup_response_has_defaults_only(authed_client, owned, monkeypatch):
    from services import matchup_service
    data = MatchupData(matchup_period=4, matchup_period_start="", matchup_period_end="",
                       your_team=_team("You", 1200.5), opponent_team=_team("Opp", 1100.0),
                       projected_winner="You", projected_margin=100.5)

    async def fake(user_id, team_id, avg_window):
        return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=data)

    monkeypatch.setattr(matchup_service.MatchupService, "get_matchup_by_team_id", staticmethod(fake))
    d = authed_client.get("/v1/internal/matchups/current/7").json()["data"]
    assert d["scoring_format"] == "points" and d["settings_synced"] is False
    assert d["category_comparison"] is None and d["your_team"]["categories"] is None
    assert d["your_team"]["current_score"] == 1200.5
