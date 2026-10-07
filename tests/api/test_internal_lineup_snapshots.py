"""GET /teams/{id}/lineup-snapshots[/{date}]: query shapes and the error mapping."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.errors import BadRequestError, NotFoundError
from schemas.common import ApiStatus, FantasyProvider
from schemas.lineup_snapshots import (
    LineupSnapshot, LineupSnapshotListData, LineupSnapshotListResp, LineupSnapshotPlayer, LineupSnapshotResp,
)


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


def _snapshot():
    return LineupSnapshot(
        provider=FantasyProvider.ESPN, provider_league_id="552315826", season=2027, provider_team_id=1,
        team_name="GloatingSoap369", scoring_period_id=21, nba_date="2026-11-09", source="snapshot",
        captured_at="2026-11-10T08:05:00+00:00",
        players=[LineupSnapshotPlayer(player_id=4432166, nba_player_id=1630595, name="Cade Cunningham", team="DET",
                                      position="PG", lineup_slot_id=0, lineup_slot="PG", eligible_slots=["PG", "UT"])],
    )


@pytest.mark.api
def test_single_day_passes_the_date_and_optional_opponent(authed_client, owned, monkeypatch):
    from services import lineup_snapshot_service as svc
    calls = []

    async def fake(team, target, provider_team_id=None):
        calls.append((team.team_id, target, provider_team_id))
        return LineupSnapshotResp(status=ApiStatus.SUCCESS, message="ok", data=_snapshot())

    monkeypatch.setattr(svc.LineupSnapshotService, "get_day", staticmethod(fake))
    body = authed_client.get("/v1/internal/teams/7/lineup-snapshots/2026-11-09?provider_team_id=5").json()
    assert body["status"] == "success"
    assert body["data"]["players"][0]["lineup_slot"] == "PG" and body["data"]["source"] == "snapshot"
    assert calls == [(7, date(2026, 11, 9), 5)]


@pytest.mark.api
def test_single_day_errors_map_to_http(authed_client, owned, monkeypatch):
    from services import lineup_snapshot_service as svc

    async def missing(team, target, provider_team_id=None):
        raise NotFoundError("LINEUP_SNAPSHOT_NOT_FOUND", "nothing stored")

    monkeypatch.setattr(svc.LineupSnapshotService, "get_day", staticmethod(missing))
    res = authed_client.get("/v1/internal/teams/7/lineup-snapshots/2026-11-09")
    assert res.status_code == 404 and res.json()["error_code"] == "LINEUP_SNAPSHOT_NOT_FOUND"

    async def future(team, target, provider_team_id=None):
        raise BadRequestError("DATE_NOT_PAST", "not yet")

    monkeypatch.setattr(svc.LineupSnapshotService, "get_day", staticmethod(future))
    assert authed_client.get("/v1/internal/teams/7/lineup-snapshots/2026-11-20").status_code == 400
    assert authed_client.get("/v1/internal/teams/7/lineup-snapshots/nov-9").status_code == 422


@pytest.mark.api
def test_range_passes_from_and_to(authed_client, owned, monkeypatch):
    from services import lineup_snapshot_service as svc
    calls = []

    async def fake(team, from_date, to_date):
        calls.append((from_date, to_date))
        return LineupSnapshotListResp(status=ApiStatus.SUCCESS, message="ok", data=LineupSnapshotListData(
            team_id=7, provider_team_id=1, from_date="2026-11-09", to_date="2026-11-15",
            snapshots=[_snapshot()], missing_dates=["2026-11-10"]))

    monkeypatch.setattr(svc.LineupSnapshotService, "list_range", staticmethod(fake))
    body = authed_client.get("/v1/internal/teams/7/lineup-snapshots?from=2026-11-09&to=2026-11-15").json()
    assert body["data"]["missing_dates"] == ["2026-11-10"] and len(body["data"]["snapshots"]) == 1
    authed_client.get("/v1/internal/teams/7/lineup-snapshots")
    assert calls == [(date(2026, 11, 9), date(2026, 11, 15)), (None, None)]
