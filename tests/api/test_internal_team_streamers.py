"""
POST /v1/internal/teams/{id}/streamers/find over the test app: ownership via the
usual `ensure_team_owned` stub, the credential loader stubbed at the route, and
the service replaced per test. The body carries search options only — the
league (with credentials) reaches the service from the team, with `team_id`.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.v1.internal import team_streamers as routes
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.streamer import StreamerData, StreamerMode, StreamerPlayerResp, StreamerResp
from services import streamer_service as svc

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="x", swid="{y}")


def _pick(pid, status=None, until=None):
    return StreamerPlayerResp(player_id=pid, name=f"P{pid}", team="DEN", valid_positions=["PG"], avg_points_season=10.0,
                              games_remaining=3, has_b2b=False, b2b_game_count=0, game_days=[0, 1], streamer_score=30.0,
                              injured=False, acquisition_status=status, waivers_until=until)


RESULT = StreamerResp(status=ApiStatus.SUCCESS, message="Found 2 streaming candidates", data=StreamerData(
    matchup_number=1, current_day_index=0, game_span=6, start_date=date(2026, 10, 20), end_date=date(2026, 10, 25),
    upcoming=True, avg_days=7, mode=StreamerMode.WEEK, teams_with_b2b=["DEN"],
    streamers=[_pick(1, "free_agent"), _pick(2, "waivers", date(2026, 10, 25))],
))


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

    async def fake(**kwargs):
        calls.append(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(svc.StreamerService, "find_streamers", staticmethod(fake))
    return calls


@pytest.mark.api
def test_the_team_supplies_the_league_and_the_body_the_options(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, RESULT)

    r = authed_client.post("/v1/internal/teams/7/streamers/find", json={"mode": "daily", "target_day": 2, "fa_count": 50})

    assert r.status_code == 200
    assert calls == [{"league_info": ESPN, "fa_count": 50, "exclude_injured": True, "b2b_only": False,
                      "mode": StreamerMode.DAILY, "target_day": 2, "avg_days": 7, "team_id": 7}]
    body = r.json()["data"]
    assert (body["start_date"], body["end_date"], body["upcoming"]) == ("2026-10-20", "2026-10-25", True)
    assert [(p["acquisition_status"], p["waivers_until"]) for p in body["streamers"]] == [
        ("free_agent", None), ("waivers", "2026-10-25")]


@pytest.mark.api
def test_a_league_info_in_the_body_is_ignored(authed_client, owned, monkeypatch):
    """The team's stored credentials are the only ones that reach the provider."""
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, RESULT)
    other = {"provider": "espn", "league_id": 999, "team_name": "X", "year": 2027, "espn_s2": "stolen", "swid": "{z}"}
    r = authed_client.post("/v1/internal/teams/7/streamers/find", json={"league_info": other})
    assert r.status_code == 200 and calls[0]["league_info"] == ESPN


@pytest.mark.api
def test_an_empty_body_uses_the_defaults(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, StreamerResp(status=ApiStatus.SUCCESS, message="No matchup on the calendar", data=None))
    r = authed_client.post("/v1/internal/teams/7/streamers/find", json={})
    assert r.status_code == 200 and r.json()["data"] is None
    assert calls[0]["fa_count"] == 300 and calls[0]["mode"] == StreamerMode.WEEK and calls[0]["team_id"] == 7


@pytest.mark.api
def test_unowned_team_is_404_before_any_provider_call(authed_client, owned, monkeypatch):
    from api import deps
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: None)
    calls = _stub(monkeypatch, RESULT)
    r = authed_client.post("/v1/internal/teams/8/streamers/find", json={})
    assert r.status_code == 404 and r.json()["error_code"] == "TEAM_NOT_FOUND" and calls == []


@pytest.mark.api
def test_option_bounds_are_fastapi_422(authed_client, owned, monkeypatch):
    _league(monkeypatch, ESPN)
    calls = _stub(monkeypatch, RESULT)
    r = authed_client.post("/v1/internal/teams/7/streamers/find", json={"fa_count": 1})
    assert r.status_code == 422 and calls == []


@pytest.mark.api
def test_provider_errors_keep_their_statuses(authed_client, owned, monkeypatch):
    from core.errors import ProviderAuthError
    _league(monkeypatch, ESPN)
    _stub(monkeypatch, ProviderAuthError("espn"))
    r = authed_client.post("/v1/internal/teams/7/streamers/find", json={})
    assert r.status_code == 403 and r.headers["X-Error-Code"] == "PROVIDER_AUTH_EXPIRED"
