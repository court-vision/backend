"""
GET /v1/internal/teams/{id}/actions over the test app: ownership via the usual
`ensure_team_owned` stub, the credential loader stubbed at the route, and the
service replaced per test. The route only hydrates the league and hands it on.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import lineup_editor as routes
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.daily_actions import DailyAction, DailyActionPlayer, DailyActionsData, DailyActionsResp
from schemas.lineup_editor import LineupMoveResult, LineupState
from services import daily_actions_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")
BOARD = LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=3, nba_date="2026-10-20",
                    scoring_period_id=1, scoring_period_source="provider", slot_counts={"11": 1, "12": 1},
                    players=[], can_write=True, roster_version="v1", fetched_at="now")
ROW = DailyAction(
    id="start:11", kind="start", title="Start P11", detail="vs LAL · 7:30 PM · UT",
    player=DailyActionPlayer(player_id=11, name="P11", team="DEN", lineup_slot_id=12, lineup_slot="BE"),
    moves=[LineupMoveResult(player_id=11, name="P11", from_slot_id=12, from_slot="BE", to_slot_id=11, to_slot="UT",
                            role="start", note="vs LAL · 7:30 PM")],
    game_time_et="19:30",
)
RESULT = DailyActionsResp(status=ApiStatus.SUCCESS, message="1 action(s) today", data=DailyActionsData(
    lineup=BOARD, roster_version="v1", scoring_period_id=1, nba_date="2026-10-20", can_write=True,
    actions=[ROW], streamers_error="day_mismatch"))


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


def _league(monkeypatch, league_info):
    async def hydrate(team):
        return league_info
    monkeypatch.setattr(routes, "load_owned_league_info", hydrate)


def _stub(monkeypatch, outcome):
    calls = []

    async def fake(team, league_info, **kwargs):
        calls.append((team.team_id, league_info))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(svc.DailyActionsService, "read", staticmethod(fake))
    return calls


@pytest.mark.api
def test_the_rows_and_the_board_come_back_together(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, RESULT)
    r = authed_client.get("/v1/internal/teams/7/actions")
    assert r.status_code == 200 and calls == [(7, ESPN)]
    body = r.json()["data"]
    assert body["lineup"]["roster_version"] == "v1" and body["can_write"] is True
    assert [(a["kind"], a["id"], a["moves"][0]["role"]) for a in body["actions"]] == [("start", "start:11", "start")]
    assert body["streamers_error"] == "day_mismatch"


@pytest.mark.api
def test_a_non_espn_answer_is_a_populated_read_only_data(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, DailyActionsResp(status=ApiStatus.SUCCESS, message=svc.NOT_ESPN_MESSAGE, data=DailyActionsData(
        can_write=False, write_blocked_reason="provider_not_supported")))
    r = authed_client.get("/v1/internal/teams/7/actions")
    body = r.json()["data"]
    assert r.status_code == 200 and body["lineup"] is None and body["actions"] == []
    assert body["write_blocked_reason"] == "provider_not_supported"


@pytest.mark.api
def test_unowned_team_is_404_before_any_provider_call(authed_client, owned, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: None)
    calls = _stub(monkeypatch, RESULT)
    r = authed_client.get("/v1/internal/teams/8/actions")
    assert r.status_code == 404 and r.json()["error_code"] == "TEAM_NOT_FOUND" and calls == []


@pytest.mark.api
def test_provider_errors_keep_their_statuses(authed_client, owned, monkeypatch):
    from core.errors import ProviderAuthError
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, ProviderAuthError("espn"))
    r = authed_client.get("/v1/internal/teams/7/actions")
    assert r.status_code == 403 and r.headers["X-Error-Code"] == "PROVIDER_AUTH_EXPIRED"
