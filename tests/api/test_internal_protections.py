"""
/v1/internal/teams/{id}/protections over the test app (the roster lock; services/roster_protection.py):
the list, protecting (and what it cancels), unprotecting. Ownership via the usual `ensure_team_owned`
stub; the rows live in memory, ESPN's board is a stub, and the lane stop and pickup cancel are faked
at their service boundaries, so the routes' plumbing, the answer's shape and every documented status
are exercised.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from services import roster_protection

EDWARDS, JOKIC, KAWHI = 4594268, 3112335, 6450
AT = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027, espn_s2="s2", swid="{S}")


def board_player(pid, name, team):
    return LineupPlayer(player_id=pid, nba_player_id=pid + 1, name=name, team=team, lineup_slot_id=12,
                        lineup_slot="BE", eligible_slot_ids=[12], eligible_slots=["BE"])


BOARD = LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=1, nba_date="2026-11-15",
                    scoring_period_id=27, roster_version="v", fetched_at="now", can_write=True,
                    players=[board_player(EDWARDS, "Anthony Edwards", "MIN"), board_player(JOKIC, "Nikola Jokic", "DEN")])


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from api.v1.internal import protections as route
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))

    async def league_info(team):
        return LEAGUE
    monkeypatch.setattr(route, "load_owned_league_info", league_info)


@pytest.fixture
def store(monkeypatch):
    """The team's rows in memory, and the lanes and pickups protecting would cancel."""
    from services import providers, scheduled_pickup_service, stream_lane_service
    from services.stream_lane_service import StreamLaneService

    st = SimpleNamespace(rows=[], lanes=[], pickups=[], stopped=[], cancelled=[], busy_lanes=set(), prior={},
                         board=BOARD)

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def insert(team_id, user_id, player_id, nba_player_id, name, team):
        found = next((r for r in st.rows if r.player_id == player_id), None)
        if found is not None:
            return found, False
        row = SimpleNamespace(id=len(st.rows) + 1, team_id_id=team_id, player_id=player_id,
                              nba_player_id=nba_player_id, player_name=name, player_team=team, created_at=AT)
        st.rows.append(row)
        return row, True

    def delete(team_id, player_id):
        found = next((r for r in st.rows if r.player_id == player_id), None)
        if found is not None:
            st.rows.remove(found)
        return found

    async def stop(team, lane_id, proposal_id=None, *, by="user", why=None):
        if lane_id in st.busy_lanes:
            raise stream_lane_service.StreamLaneBusy()
        st.stopped.append((lane_id, why))

    def cancel(team_id, pickup_id, now):
        st.cancelled.append(pickup_id)
        return st.prior.get(pickup_id, "pending"), SimpleNamespace(id=pickup_id)

    class Adapter:
        async def read_lineup(self, team_id, league_info, **kw):
            return st.board

    monkeypatch.setattr(roster_protection, "run_db", direct)
    monkeypatch.setattr(roster_protection, "_rows", lambda team_id: sorted(st.rows, key=lambda r: r.player_name))
    monkeypatch.setattr(roster_protection, "_insert", insert)
    monkeypatch.setattr(roster_protection, "_delete", delete)
    monkeypatch.setattr(roster_protection, "_lanes_holding",
                        lambda team_id, player_id: [ln for ln in st.lanes if ln.holder_player_id == player_id])
    monkeypatch.setattr(roster_protection, "_pickups_dropping",
                        lambda team_id, player_id: [p for p in st.pickups if p.drop_player_id == player_id])
    monkeypatch.setattr(StreamLaneService, "stop", staticmethod(stop))
    monkeypatch.setattr(scheduled_pickup_service, "_cancel_pickup", cancel)
    monkeypatch.setattr(providers, "get_provider_adapter", lambda provider: Adapter())
    return st


@pytest.mark.api
def test_protecting_a_player_lists_him(authed_client, owned, store):
    r = authed_client.put(f"/v1/internal/teams/7/protections/{EDWARDS}")
    assert r.status_code == 200 and r.json()["message"] == "Protected Anthony Edwards"
    data = r.json()["data"]
    assert data["players"] == [{"espn_id": EDWARDS, "nba_player_id": EDWARDS + 1, "name": "Anthony Edwards",
                                "team": "MIN", "protected_at": "2026-11-15T17:00:00Z"}]
    assert data["cancelled"] == [] and data["not_cancelled"] == []
    # Protecting him again changes nothing
    again = authed_client.put(f"/v1/internal/teams/7/protections/{EDWARDS}")
    assert again.status_code == 200 and len(again.json()["data"]["players"]) == 1
    listed = authed_client.get("/v1/internal/teams/7/protections")
    assert listed.status_code == 200 and [p["espn_id"] for p in listed.json()["data"]["players"]] == [EDWARDS]


@pytest.mark.api
def test_protecting_cancels_the_pickups_dropping_him_and_stops_the_lanes_on_his_spot(authed_client, owned, store):
    """James's pick (2026-11-15): what would drop him goes, and the answer names each."""
    store.lanes = [SimpleNamespace(id=3, holder_player_id=EDWARDS)]
    store.pickups = [SimpleNamespace(id=11, drop_player_id=EDWARDS, nba_date=date(2026, 11, 19), add_name="Kawhi Leonard"),
                     SimpleNamespace(id=12, drop_player_id=EDWARDS, nba_date=date(2026, 11, 20), add_name="Joel Embiid")]
    store.prior = {12: "in_progress"}
    r = authed_client.put(f"/v1/internal/teams/7/protections/{EDWARDS}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert store.stopped == [(3, {"reason": "stopped_for_protected", "detail": None})]
    assert store.cancelled == [11, 12]
    assert data["cancelled"] == [
        {"kind": "lane", "id": 3, "summary": "the stream lane on Anthony Edwards's spot"},
        {"kind": "pickup", "id": 11, "summary": "Thu's pickup of Kawhi Leonard"},
    ]
    assert data["not_cancelled"] == ["Fri's pickup of Joel Embiid"]
    assert r.json()["message"] == ("Protected Anthony Edwards; cancelled the stream lane on Anthony Edwards's spot and "
                                   "Thu's pickup of Kawhi Leonard; Fri's pickup of Joel Embiid was already being sent")


@pytest.mark.api
def test_a_lane_whose_pickup_is_being_made_is_named_as_not_cancelled(authed_client, owned, store):
    store.lanes = [SimpleNamespace(id=3, holder_player_id=EDWARDS)]
    store.busy_lanes = {3}
    data = authed_client.put(f"/v1/internal/teams/7/protections/{EDWARDS}").json()["data"]
    assert data["cancelled"] == [] and data["not_cancelled"] == ["the stream lane's pickup dropping Anthony Edwards"]
    assert [p["espn_id"] for p in data["players"]] == [EDWARDS]            # protected all the same


@pytest.mark.api
def test_only_a_player_on_the_roster_can_be_protected(authed_client, owned, store):
    r = authed_client.put(f"/v1/internal/teams/7/protections/{KAWHI}")
    assert r.status_code == 422 and r.json()["error_code"] == "PROTECTION_NOT_ON_ROSTER"
    assert store.rows == []


@pytest.mark.api
def test_unprotecting(authed_client, owned, store):
    authed_client.put(f"/v1/internal/teams/7/protections/{EDWARDS}")
    r = authed_client.delete(f"/v1/internal/teams/7/protections/{EDWARDS}")
    assert r.status_code == 200 and r.json()["data"]["players"] == []
    assert r.json()["message"] == "Anthony Edwards is no longer protected"
    r = authed_client.delete(f"/v1/internal/teams/7/protections/{EDWARDS}")
    assert r.status_code == 404 and r.json()["error_code"] == "PROTECTION_NOT_FOUND"
