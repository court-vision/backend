"""
LineupService end to end, without the network or a database:

- the shared features client (services.features_client) through an
  httpx.MockTransport: payload shape, response parsing, the 400 -> ERROR mapping,
  one retry on a connection failure, and the "unavailable" message once retries
  are exhausted (with the structured log line per attempt);
- fetch_roster_and_fas over stubbed ESPN/Yahoo services, including a team whose
  scoring_preview renders it as a category league.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from structlog.testing import capture_logs

from db.models import Team
from schemas.common import ApiStatus, FantasyProvider
from schemas.espn import PlayerResp, TeamDataResp
from schemas.lineup import LineupInfo
from schemas.optimize import GenerateLineupRequest
from services import espn_service, features_client, lineup_service
from services.espn_service import EspnService
from services.features_client import UNAVAILABLE_MESSAGE
from services.lineup_service import LineupService
from services.optimize_service import OptimizeService
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.resolver import resolve_scoring
from services.yahoo_service import YahooService
from utils.constants import NUM_FREE_AGENTS


def _player(pid: int, name: str, avg: float, team: str = "DEN", kind: str = "fpts", source: str = "rolling") -> PlayerResp:
    return PlayerResp(player_id=pid, name=name, avg_points=avg, team=team, valid_positions=["PG", "G", "UT1"],
                      injured=False, value_kind=kind, value_source=source)


ROSTER = [_player(1, "Nikola Jokić", 60.0), _player(2, "Role Player", 12.0, "LAL")]
FAS = [_player(3, "Streamer", 20.0, "BOS")]


def _v2_response(week: int, days: int = 7) -> dict:
    roster = {"PG": {"Name": "Nikola Jokić", "AvgPoints": 60.0, "Team": "DEN"}}
    return {
        "Lineup": [{"Day": d, "Additions": [], "Removals": [], "Roster": roster} for d in range(days)],
        "Improvement": 42, "Timestamp": "8/25/2026 10:00AM", "Week": week, "StreamingSlots": 2,
    }


UNKNOWN_WEEK = httpx.Response(400, json={"error": "unknown week", "week": 99, "weeks": 24})


@pytest.fixture
def features(monkeypatch):
    """Serve the features service from a MockTransport. `responses` is a queue of
    httpx.Response objects or exceptions to raise; `requests` records what was sent."""
    state = SimpleNamespace(responses=[], requests=[])

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        nxt = state.responses.pop(0) if state.responses else httpx.Response(200, json=_v2_response(3))
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(features_client, "client_factory",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://features.test"))
    monkeypatch.setattr(features_client, "RETRY_DELAY_SECONDS", 0)
    return state


def _events(logs, name):
    return [e for e in logs if e["event"] == name]


# ---- features client ------------------------------------------------------------------


@pytest.mark.unit
def test_success_path_posts_the_v2_payload_and_parses_the_lineup(features):
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert isinstance(resp.data, LineupInfo)
    assert resp.data.Week == 3 and resp.data.Improvement == 42 and len(resp.data.Lineup) == 7

    assert len(features.requests) == 1
    request = features.requests[0]
    assert request.method == "POST" and request.url.path == "/generate-lineup"
    body = json.loads(request.content)
    assert set(body) == {"roster_data", "free_agent_data", "streaming_slots", "week"}
    assert (body["streaming_slots"], body["week"]) == (2, 3)
    assert [p["name"] for p in body["roster_data"]] == ["Nikola Jokić", "Role Player"]
    assert [p["player_id"] for p in body["free_agent_data"]] == [3]
    # The Go struct reads name/avg_points/team/valid_positions/injured; extra fields are ignored
    assert {"name", "avg_points", "team", "valid_positions", "injured", "value_kind"} <= set(body["roster_data"][0])


@pytest.mark.unit
def test_a_features_400_is_an_error_response_carrying_its_message(features):
    features.responses = [UNKNOWN_WEEK]

    with capture_logs() as logs:
        resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 99))

    assert resp.status == ApiStatus.ERROR
    assert resp.message == "unknown week 99 (calendar has 24 weeks)"
    assert resp.error_code == "LINEUP_SERVICE_REJECTED" and resp.data is None
    assert len(features.requests) == 1                                    # a rejection is never retried
    rejected = _events(logs, "features_generate_lineup_rejected")
    assert rejected and rejected[0]["status_code"] == 400 and rejected[0]["week"] == 99


@pytest.mark.unit
def test_a_plain_text_400_and_a_422_still_surface_the_service_text(features):
    features.responses = [httpx.Response(400, text="Failed to decode request body\n")]
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == "Failed to decode request body"

    features.responses = [httpx.Response(422, json={"error": "streaming_slots must be positive"})]
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == "streaming_slots must be positive"


@pytest.mark.unit
def test_one_connect_error_then_success_is_a_success_with_two_attempts_logged(features):
    features.responses = [httpx.ConnectError("connection refused"), httpx.Response(200, json=_v2_response(18, 14))]

    with capture_logs() as logs:
        resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 18))

    assert resp.status == ApiStatus.SUCCESS and len(resp.data.Lineup) == 14
    assert len(features.requests) == 2

    retry = _events(logs, "features_generate_lineup_retry")
    done = _events(logs, "features_generate_lineup")
    assert [e["attempt"] for e in retry] == [1] and [e["attempt"] for e in done] == [2]
    assert retry[0]["error"] == "ConnectError" and retry[0]["log_level"] == "warning"
    for event in (retry[0], done[0]):
        assert event["week"] == 18 and event["roster"] == 2 and event["free_agents"] == 1
        assert event["streaming_slots"] == 2 and event["value_kind"] == "fpts"
        assert isinstance(event["elapsed_ms"], int) and event["caller"] == "lineup_service"
    assert done[0]["status_code"] == 200


@pytest.mark.unit
def test_two_connect_errors_map_to_the_unavailable_message(features):
    features.responses = [httpx.ConnectError("refused"), httpx.ConnectError("refused again")]

    with capture_logs() as logs:
        resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))

    assert resp.status == ApiStatus.ERROR
    assert resp.message == UNAVAILABLE_MESSAGE == "Lineup service unavailable, try again in a minute"
    assert resp.error_code == "LINEUP_SERVICE_UNAVAILABLE" and resp.data is None
    assert len(features.requests) == 2
    failed = _events(logs, "features_generate_lineup_unavailable")
    assert len(failed) == 1 and failed[0]["attempt"] == 2 and failed[0]["log_level"] == "error"


@pytest.mark.unit
def test_timeouts_and_dropped_connections_retry_but_a_5xx_does_not(features):
    features.responses = [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("closed"), None]
    features.responses.pop()
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == UNAVAILABLE_MESSAGE
    assert len(features.requests) == 2

    features.requests.clear()
    features.responses = [httpx.Response(500, text="internal error")]
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == UNAVAILABLE_MESSAGE
    assert resp.error_code == "LINEUP_SERVICE_UNAVAILABLE" and len(features.requests) == 1

    features.requests.clear()
    features.responses = [httpx.Response(200, text="not json")]
    resp = asyncio.run(LineupService.generate_lineup_v2(ROSTER, FAS, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == UNAVAILABLE_MESSAGE


@pytest.mark.unit
def test_the_shared_timeout_is_generous_to_read_and_quick_to_connect():
    assert features_client.FEATURES_TIMEOUT.read == 90.0
    assert features_client.FEATURES_TIMEOUT.connect == 15.0
    assert features_client.MAX_ATTEMPTS == 2


@pytest.mark.unit
def test_optimize_service_uses_the_same_client_and_error_mapping(features, monkeypatch):
    async def fake_fetch(user_id, team_id, use_recent_stats=False, scoring_preview=None):
        return ROSTER, FAS

    monkeypatch.setattr(LineupService, "fetch_roster_and_fas", staticmethod(fake_fetch))
    api_key = SimpleNamespace(user_id=42)
    request = GenerateLineupRequest(team_id=15, week=3, streaming_slots=2, use_recent_stats=False)

    resp = asyncio.run(OptimizeService.optimize_from_team(api_key, request))
    assert resp.status == ApiStatus.SUCCESS and resp.data.week == 3 and len(resp.data.daily_lineups) == 7
    body = json.loads(features.requests[-1].content)
    assert set(body) == {"roster_data", "free_agent_data", "streaming_slots", "week"}

    features.responses = [UNKNOWN_WEEK]
    resp = asyncio.run(OptimizeService.optimize_from_team(api_key, request))
    assert resp.status == ApiStatus.ERROR and resp.message == "unknown week 99 (calendar has 24 weeks)"
    assert resp.error_code == "LINEUP_SERVICE_REJECTED"

    features.responses = [httpx.ConnectError("x"), httpx.ConnectError("y")]
    resp = asyncio.run(OptimizeService.optimize_from_team(api_key, request))
    assert resp.status == ApiStatus.ERROR and resp.message == UNAVAILABLE_MESSAGE


# ---- fetch_roster_and_fas ------------------------------------------------------------


def _stored_team(**league_info) -> SimpleNamespace:
    base = {"league_id": 555, "team_name": "My Team", "year": 2026, "espn_s2": "s2", "swid": "{swid}"}
    return SimpleNamespace(team_id=15, league_info=json.dumps({**base, **league_info}))


@pytest.fixture
def team(monkeypatch):
    """`state.team` is the stored row `_owned_team` returns (None -> Team.DoesNotExist)."""
    state = SimpleNamespace(team=_stored_team(), owner_calls=[])

    def owned(user_id, team_id):
        state.owner_calls.append((user_id, team_id))
        if state.team is None:
            raise Team.DoesNotExist()
        return state.team

    monkeypatch.setattr(LineupService, "_owned_team", staticmethod(owned))
    return state


@pytest.fixture
def providers(monkeypatch):
    """Stub the ESPN and Yahoo fetches; `calls` records (service, method, args)."""
    state = SimpleNamespace(calls=[], roster=TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=list(ROSTER)),
                            fas=TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=list(FAS)))

    def stub(service, method, which):
        async def fake(*args, **kwargs):
            state.calls.append((service, method, args, kwargs))
            return getattr(state, which)
        return staticmethod(fake)

    monkeypatch.setattr(EspnService, "get_team_data", stub("espn", "team", "roster"))
    monkeypatch.setattr(EspnService, "get_free_agents", stub("espn", "fas", "fas"))
    monkeypatch.setattr(YahooService, "get_team_data", stub("yahoo", "team", "roster"))
    monkeypatch.setattr(YahooService, "get_free_agents", stub("yahoo", "fas", "fas"))
    return state


@pytest.mark.unit
def test_espn_team_gets_roster_and_free_agents_from_its_stored_credentials(team, providers):
    roster, fas = asyncio.run(LineupService.fetch_roster_and_fas(42, 15))

    assert team.owner_calls == [(42, 15)]
    assert [p.name for p in roster] == ["Nikola Jokić", "Role Player"] and [p.name for p in fas] == ["Streamer"]
    (svc, method, args, _), (svc2, method2, args2, _) = providers.calls
    assert (svc, method) == ("espn", "team") and (svc2, method2) == ("espn", "fas")
    league_info = args[0]
    assert league_info.provider == FantasyProvider.ESPN and league_info.league_id == 555
    assert league_info.year == 2026 and league_info.espn_s2 == "s2" and league_info.scoring_preview is None
    assert args2 == (league_info, NUM_FREE_AGENTS)


@pytest.mark.unit
def test_yahoo_team_uses_team_scoped_calls_and_revalues_recent_form_by_name(team, providers, monkeypatch):
    team.team = _stored_team(provider="yahoo", league_id=368978, yahoo_team_key="466.l.368978.t.1",
                             yahoo_access_token="tok", yahoo_refresh_token="ref")
    seen = {}
    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(lambda li, team_id=None: resolve_scoring(None)))

    def fake_values(scoring, *, espn_ids=None, names=None, days=14, recent=False):
        seen.update(names=names, espn_ids=espn_ids, days=days, recent=recent, kind=PlayerValueService.value_kind_for(scoring))
        return {"nikola jokic": ValueResult(55.0, "recent")}

    monkeypatch.setattr(PlayerValueService, "avg_points_for", staticmethod(fake_values))

    roster, fas = asyncio.run(LineupService.fetch_roster_and_fas(42, 15, use_recent_stats=True))

    (svc, method, args, _), (svc2, method2, args2, _) = providers.calls
    assert (svc, method, args[1:]) == ("yahoo", "team", (0, 15))
    assert (svc2, method2, args2[1:]) == ("yahoo", "fas", (NUM_FREE_AGENTS, 15))
    assert seen["names"] == [("Nikola Jokić", "DEN"), ("Role Player", "LAL"), ("Streamer", "BOS")]
    assert seen["espn_ids"] is None and seen["recent"] is True and seen["kind"] == "fpts"
    by_name = {p.name: p for p in roster + fas}
    assert by_name["Nikola Jokić"].avg_points == 55.0 and by_name["Nikola Jokić"].value_source == "recent"
    assert by_name["Role Player"].avg_points == 12.0 and by_name["Role Player"].value_source == "rolling"  # unknown: keeps provider's


@pytest.mark.unit
def test_provider_failure_and_unknown_team_become_error_responses(team, providers):
    providers.fas = TeamDataResp(status=ApiStatus.ERROR, message="Internal server error", data=None)
    with pytest.raises(ValueError):
        asyncio.run(LineupService.fetch_roster_and_fas(42, 15))
    resp = asyncio.run(LineupService.generate_lineup(42, 15, 2, 3))
    assert resp.status == ApiStatus.ERROR and "provider" in resp.message

    team.team = None
    with pytest.raises(Team.DoesNotExist):
        asyncio.run(LineupService.fetch_roster_and_fas(42, 15))
    resp = asyncio.run(LineupService.generate_lineup(42, 15, 2, 3))
    assert resp.status == ApiStatus.ERROR and resp.message == "Team not found"


# A minimal ESPN payload so the real EspnService parsing runs (HTTP stubbed).
YEAR = 2026


def _espn_player(pid: int, name: str, applied_avg: float, pro_team_id: int = 7) -> dict:
    split = {"seasonId": YEAR, "id": f"00{YEAR}", "scoringPeriodId": 0,
             "appliedTotal": applied_avg * 10, "appliedAverage": applied_avg}
    return {"id": pid, "fullName": name, "defaultPositionId": 1, "eligibleSlots": [0, 5],
            "proTeamId": pro_team_id, "injuryStatus": "ACTIVE", "injured": False, "stats": [split]}


ROSTER_PAYLOAD = {"teams": [{"id": 1, "name": "My Team", "roster": {"entries": [
    {"lineupSlotId": 0, "playerPoolEntry": {"player": _espn_player(1, "Nikola Jokić", 30.0)}},
    {"lineupSlotId": 0, "playerPoolEntry": {"player": _espn_player(2, "Role Player", 12.5)}},
    {"lineupSlotId": 0, "playerPoolEntry": {"player": _espn_player(3, "Category Guy", 0.0)}},
]}}]}
FA_PAYLOAD = {"players": [{"id": 4, "player": _espn_player(4, "Streamer", 9.0)},
                          {"id": 5, "player": _espn_player(5, "Nobody", 0.0)}]}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def espn_http(monkeypatch):
    """Real ESPN parsing over canned payloads; scoring resolves like the resolver minus the DB."""
    payloads = iter([ROSTER_PAYLOAD, FA_PAYLOAD])
    monkeypatch.setattr(espn_service.requests, "get", lambda *a, **k: _Resp(next(payloads)))
    calls = []
    monkeypatch.setattr(PlayerValueService, "scoring_for",
                        staticmethod(lambda li, team_id=None: (calls.append(("scoring_for", team_id, li.scoring_preview)),
                                                               resolve_scoring(None, li.scoring_preview))[1]))

    def fake_values(scoring, *, espn_ids=None, names=None, days=14, recent=False):
        calls.append(("avg_points_for", PlayerValueService.value_kind_for(scoring), list(espn_ids or []), recent))
        if scoring.is_categories:   # category values: fpts-scale, 0..100, some players unknown
            table = {1: ValueResult(61.5, "baseline"), 2: ValueResult(0.0, "baseline"), 4: ValueResult(17.2, "baseline")}
        else:
            table = {1: ValueResult(58.0, "rolling"), 4: ValueResult(11.0, "rolling")}
        return {eid: table.get(eid, ValueResult(None, None)) for eid in espn_ids}

    monkeypatch.setattr(PlayerValueService, "avg_points_for", staticmethod(fake_values))
    return calls


@pytest.mark.unit
def test_stored_category_preview_values_every_player_as_cat_value(team, espn_http):
    team.team = _stored_team(scoring_preview="categories")

    roster, fas = asyncio.run(LineupService.fetch_roster_and_fas(42, 15))

    players = roster + fas
    assert [p.name for p in roster] == ["Nikola Jokić", "Role Player", "Category Guy"]
    assert [p.name for p in fas] == ["Streamer", "Nobody"]
    assert all(p.value_kind == "cat_value" for p in players)
    assert all(p.avg_points >= 0 for p in players)
    by_id = {p.player_id: p for p in players}
    assert by_id[1].avg_points == 61.5 and by_id[1].value_source == "baseline"
    assert by_id[2].avg_points == 0.0                                    # a real 0 is kept, not the provider's number
    assert by_id[3].avg_points == 0.0 and by_id[3].value_source == "provider"   # ESPN's appliedAverage for a cat league
    assert by_id[4].avg_points == 17.2 and by_id[5].value_source == "provider"
    assert [c for c in espn_http if c[0] == "avg_points_for"] == [
        ("avg_points_for", "cat_value", [1, 2, 3], False), ("avg_points_for", "cat_value", [4, 5], False)]


@pytest.mark.unit
def test_in_process_preview_overrides_the_stored_setting_without_writing_it(team, espn_http):
    stored = team.team.league_info
    roster, fas = asyncio.run(LineupService.fetch_roster_and_fas(42, 15, use_recent_stats=True, scoring_preview="categories"))

    assert team.team.league_info == stored and "scoring_preview" not in json.loads(stored)
    assert all(p.value_kind == "cat_value" and p.avg_points >= 0 for p in roster + fas)
    # the recent-form pass resolved from league_info (team_id=None) so the preview wins over the row
    assert ("scoring_for", None, "categories") in espn_http
    assert ("avg_points_for", "cat_value", [1, 2, 3, 4, 5], True) in espn_http
    assert all(c[1] is None for c in espn_http if c[0] == "scoring_for")


@pytest.mark.unit
def test_without_a_preview_the_stored_points_league_yields_fpts(team, espn_http):
    roster, fas = asyncio.run(LineupService.fetch_roster_and_fas(42, 15))
    assert all(p.value_kind == "fpts" for p in roster + fas)
    by_id = {p.player_id: p for p in roster + fas}
    assert by_id[1].avg_points == 58.0 and by_id[2].avg_points == 12.5 and by_id[2].value_source == "provider"
