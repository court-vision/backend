"""
ESPN connections: one stored cookie pair per ESPN account, managed directly.

The rules that make one row safe to share across teams -- one spelling of a
SWID, no half pairs -- the add-team path that reuses a stored connection, the
check that runs before a pair is saved, the account's teams as ESPN lists them,
and a client-facing view that cannot carry a credential. The DB-backed halves
(the upsert, migration 0024, adopting teams) are in
tests/integration/test_provider_connections_integration.py.

`fixtures/espn_fan_account.json` is synthetic, shaped like a captured fan-API
response: two teams in one custom league, one in a public draft lobby, one from
last season, and a football team that must be ignored.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from core import crypto
from core.errors import (
    BadRequestError,
    NotFoundError,
    ProviderAuthError,
    ProviderError,
    ServiceUnavailableError,
)
from schemas.common import FantasyProvider, LeagueInfo, LeagueInfoPublic
from schemas.connections import ConnectionTeamInfo, EspnAccountTeam, ProviderConnectionInfo
from services import connection_service as cs
from services import credential_service
from services.espn_service import ESPN_ACCOUNT_NOT_FOUND, EspnService
from services.providers import http as provider_http
from services.providers.http import LEAGUE_NOT_FOUND_CODE
from services.team_service import TeamService

SWID = "{3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6}"
STORED = {"espn_s2": "AEB-STORED", "swid": SWID}
ESPN_NO_COOKIES = dict(provider=FantasyProvider.ESPN, league_id=5, team_name="T", year=2026)
NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
FAN = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "espn_fan_account.json").read_text())


@pytest.fixture
def keys(monkeypatch):
    from core.settings import settings

    monkeypatch.setattr(settings, "credential_keys", f"1:{Fernet.generate_key().decode()}", raising=False)
    crypto.reset_cache()
    yield
    crypto.reset_cache()


def _view(connection_id=21):
    return ProviderConnectionInfo(id=connection_id, provider="espn", account_hint="…E5F6",
                                  status="ok", created_at=NOW, updated_at=NOW)


@pytest.mark.unit
class TestNormalizeSwid:
    @pytest.mark.parametrize("raw", [
        SWID,
        SWID.lower(),
        SWID.strip("{}"),
        SWID.lower().strip("{}"),
        f"  {SWID}\n",
        "{ " + SWID.strip("{}") + " }",
    ])
    def test_every_spelling_is_the_canonical_one(self, raw):
        assert credential_service.normalize_swid(raw) == SWID

    @pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
    def test_empty_stays_empty(self, raw):
        assert credential_service.normalize_swid(raw) == ""

    def test_it_keys_the_connection_row(self):
        assert credential_service._external_account_id("espn", {"swid": SWID.lower()}) == SWID


@pytest.mark.unit
class TestPartialPairsAreNotStored:
    def test_half_an_espn_pair_is_stripped_but_not_written(self, keys, monkeypatch):
        """A lone SWID would replace the account's espn_s2 for every team on it."""
        writes = []
        monkeypatch.setattr(credential_service, "_upsert_connection",
                            lambda *a, **k: writes.append(a))
        saved = []
        team = SimpleNamespace(team_id=1, provider_connection_id=4, league_info="{}",
                               save=lambda: saved.append(1))
        payload = {"provider": "espn", "league_id": 5, "team_name": "T", "year": 2026, "swid": SWID}

        assert credential_service.persist(10, team, payload) == 4
        assert not writes
        assert "swid" not in json.loads(team.league_info) and saved, "the half pair stays out of league_info too"


@pytest.mark.unit
class TestAddTeamReusesTheConnection:
    @pytest.fixture
    def store(self, monkeypatch):
        state = SimpleNamespace(ids=[21], loads=[])

        def load(user_id, connection_id, provider=None):
            state.loads.append((user_id, connection_id, provider))
            return dict(STORED) if (user_id, connection_id) == (10, 21) else None

        monkeypatch.setattr(credential_service, "load_provider_tokens", load)
        monkeypatch.setattr(credential_service, "connection_ids", lambda user_id, provider: list(state.ids))
        return state

    def test_the_only_connection_is_used_by_default(self, store):
        resolved = TeamService._resolve_connection_handle(10, LeagueInfo(**ESPN_NO_COOKIES))
        assert resolved.espn_s2 == "AEB-STORED" and resolved.swid == SWID
        assert store.loads == [(10, 21, "espn")], "an ESPN handle must not resolve to a Yahoo row"

    def test_pasted_cookies_win(self, store):
        incoming = LeagueInfo(**ESPN_NO_COOKIES, espn_s2="AEB-PASTED", swid="{PASTED}")
        assert TeamService._resolve_connection_handle(10, incoming) is incoming
        assert not store.loads

    def test_an_explicit_handle_picks_among_several(self, store):
        store.ids = [21, 22]
        resolved = TeamService._resolve_connection_handle(
            10, LeagueInfo(**ESPN_NO_COOKIES, espn_connection_id=21))
        assert resolved.espn_s2 == "AEB-STORED"

    @pytest.mark.parametrize("ids", [[], [21, 22]])
    def test_no_default_without_exactly_one(self, store, ids):
        store.ids = ids
        incoming = LeagueInfo(**ESPN_NO_COOKIES)
        assert TeamService._resolve_connection_handle(10, incoming) is incoming

    def test_another_users_handle_resolves_to_nothing(self, store):
        with pytest.raises(BadRequestError) as exc:
            TeamService._resolve_connection_handle(
                99, LeagueInfo(**ESPN_NO_COOKIES, espn_connection_id=21))
        assert exc.value.error_code == "ESPN_CONNECTION_NOT_FOUND"

    def test_an_edit_is_not_silently_attached_to_an_account(self, store):
        """update_team resolves with default_espn=False: a team with no cookies
        on file (a public league) keeps having none."""
        incoming = LeagueInfo(**ESPN_NO_COOKIES)
        assert TeamService._resolve_connection_handle(10, incoming, default_espn=False) is incoming
        assert not store.loads

    def test_the_handle_is_never_persisted_or_echoed(self):
        info = LeagueInfo(**ESPN_NO_COOKIES, espn_connection_id=21)
        assert "espn_connection_id" not in TeamService.serialize_league_info(info)
        assert "espn_connection_id" not in LeagueInfoPublic.model_fields


@pytest.mark.unit
@pytest.mark.parametrize("verified_at, auth_failed_at, expected", [
    (None, None, "unknown"),
    (NOW, None, "ok"),
    (None, NOW, "expired"),
    (NOW - HOUR, NOW, "expired"),
    (NOW, NOW - HOUR, "ok"),
])
def test_status_follows_the_latest_verdict(verified_at, auth_failed_at, expected):
    assert cs.connection_status(verified_at, auth_failed_at) == expected


@pytest.mark.unit
class TestTheViewCannotCarryCredentials:
    def test_no_credential_or_storage_field(self):
        """Structural, like LeagueInfoPublic's test: a field that does not exist cannot leak."""
        fields = (set(ProviderConnectionInfo.model_fields) | set(ConnectionTeamInfo.model_fields)
                  | set(EspnAccountTeam.model_fields))
        forbidden = credential_service.ALL_SECRET_FIELDS | {"secret_ciphertext", "key_version", "external_account_id"}
        assert not fields & forbidden

    def test_the_account_hint_is_four_characters_of_the_guid(self):
        assert cs._account_hint("espn", SWID) == "…E5F6"
        assert cs._account_hint("yahoo", "") is None


@pytest.mark.unit
class TestFanTeams:
    def test_basketball_entries_only_newest_season_first(self):
        teams = cs.espn_fan_teams(FAN)
        assert [(t.season, t.league_id, t.espn_team_id, t.team_name) for t in teams] == [
            (2027, 2222222, 8, "Charlie"),
            (2027, 1111111, 1, "Alpha"),
            (2027, 1111111, 2, "Bravo"),
            (2026, 3333333, 5, "Delta"),
        ]
        alpha = teams[1]
        assert (alpha.league_name, alpha.league_size, alpha.team_abbrev, alpha.scoring_type) == (
            "Test League", 4, "ALP", "H2H_POINTS")

    def test_an_anonymous_payload_lists_nothing(self):
        assert cs.espn_fan_teams({"anon": True}) == []

    def test_malformed_entries_are_skipped(self):
        broken = {"preferences": [
            {"type": {"code": "fantasy"}, "metaData": {"entry": {"abbrev": "FBA", "groups": []}}},
            {"type": {"code": "fantasy"}, "metaData": {"entry": {"abbrev": "FBA", "seasonId": "x",
                                                                 "entryId": 1, "groups": [{"groupId": 5}]}}},
        ]}
        assert cs.espn_fan_teams(broken) == []

    def test_custom_leagues_are_probed_before_public_lobbies(self):
        assert cs._probe_leagues(FAN) == [(2027, 1111111), (2026, 3333333), (2027, 2222222)]

    def test_teams_already_tracked_are_marked(self):
        tracked = [
            (7, {"league_id": 1111111, "year": 2027, "team_name": "Renamed", "espn_team_id": 1}),
            (9, {"league_id": 3333333, "year": 2026, "team_name": "Delta "}),
        ]
        marked = {t.team_name: t.tracked_team_id for t in cs._mark_tracked(cs.espn_fan_teams(FAN), tracked)}
        assert marked == {"Charlie": None, "Alpha": 7, "Bravo": None, "Delta": 9}

    def test_a_team_saved_last_season_is_the_same_team(self):
        """Court Vision's team identity is league + name, with no season: a team
        saved for 2026 is the one ESPN now lists under the league's 2027 renewal,
        and adding it again would only say it already exists."""
        tracked = [(7, {"league_id": 1111111, "year": 2026, "team_name": "Alpha"})]
        marked = {t.team_name: t.tracked_team_id for t in cs._mark_tracked(cs.espn_fan_teams(FAN), tracked)}
        assert marked["Alpha"] == 7


@pytest.fixture
def espn(monkeypatch):
    """EspnService's two reads replaced by scripted answers; leagues default to public."""
    state = SimpleNamespace(fan=dict(FAN), fan_error=None, fan_calls=[], reads=[], answers={})

    async def fetch_fan(espn_s2, swid):
        state.fan_calls.append((espn_s2, swid))
        if state.fan_error:
            raise state.fan_error
        return state.fan

    async def fetch_league_settings(espn_s2, swid, season, league_id):
        state.reads.append(league_id)
        answer = state.answers.get(league_id, {"isPublic": True})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cs.EspnService, "fetch_fan", staticmethod(fetch_fan))
    monkeypatch.setattr(cs.EspnService, "fetch_league_settings", staticmethod(fetch_league_settings))
    return state


async def _verdict(espn_s2="AEB"):
    verdict, _ = await cs._check_espn_cookies(espn_s2, SWID)
    return verdict


@pytest.mark.unit
class TestCheckCookies:
    """ESPN's fan API answers 200 for a known SWID whatever the cookies, so the
    proof is reading one of the account's private leagues with them."""

    @pytest.mark.asyncio
    async def test_a_private_league_read_accepts_them(self, espn):
        espn.answers = {1111111: {"isPublic": False}}
        verdict, fan = await cs._check_espn_cookies("AEB", SWID)
        assert verdict == "accepted" and fan is espn.fan
        assert espn.reads == [1111111]

    @pytest.mark.asyncio
    async def test_a_refused_private_league_refuses_them(self, espn):
        espn.answers = {1111111: ProviderAuthError("espn")}
        assert await _verdict() == "refused"

    @pytest.mark.asyncio
    async def test_an_anonymous_fan_read_refuses_them_without_a_league_read(self, espn):
        espn.fan = {**FAN, "anon": True}
        assert await _verdict() == "refused"
        assert not espn.reads

    @pytest.mark.asyncio
    async def test_only_public_leagues_leave_them_unconfirmed(self, espn):
        assert await _verdict() == "unconfirmed"
        assert espn.reads == [1111111, 3333333, 2222222]

    @pytest.mark.asyncio
    async def test_a_missing_league_is_skipped(self, espn):
        espn.answers = {1111111: BadRequestError(LEAGUE_NOT_FOUND_CODE, "gone"), 3333333: {"isPublic": False}}
        assert await _verdict() == "accepted"

    @pytest.mark.asyncio
    async def test_an_outage_is_raised_not_judged(self, espn):
        espn.answers = {1111111: ProviderError("espn", "ESPN isn't responding")}
        with pytest.raises(ProviderError):
            await _verdict()

    @pytest.mark.asyncio
    async def test_reads_are_capped(self, espn, monkeypatch):
        monkeypatch.setattr(cs, "MAX_LEAGUE_PROBES", 1)
        assert await _verdict() == "unconfirmed"
        assert espn.reads == [1111111]


@pytest_asyncio.fixture
async def espn_http(monkeypatch):
    state = SimpleNamespace(queue=[], calls=[])

    async def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        status, body = state.queue.pop(0)
        return httpx.Response(status, json=body)

    await provider_http.stop_provider_runtime()
    monkeypatch.setattr(provider_http, "RETRY_BASE_DELAY", 0)
    provider_http._clients["espn"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider_http._capacity["espn"] = asyncio.Semaphore(4)
    yield state
    await provider_http.stop_provider_runtime()


@pytest.mark.unit
class TestEspnReads:
    @pytest.mark.asyncio
    async def test_the_fan_read_encodes_the_braces_and_sends_both_cookies(self, espn_http):
        """ESPN's fan API answers a literal `{` in the path with 400."""
        espn_http.queue.append((200, {"id": SWID, "anon": False}))
        assert (await EspnService.fetch_fan("AEB-cookie", SWID))["id"] == SWID
        request = espn_http.calls[0]
        assert request.url.raw_path.decode().endswith("/fans/%7B3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6%7D")
        assert request.headers["cookie"] == f"espn_s2=AEB-cookie; SWID={SWID}"

    @pytest.mark.asyncio
    async def test_an_unknown_swid_is_not_a_league_error(self, espn_http):
        espn_http.queue.append((404, {"message": "fan not found"}))
        with pytest.raises(BadRequestError) as exc:
            await EspnService.fetch_fan("AEB-cookie", SWID)
        assert exc.value.error_code == ESPN_ACCOUNT_NOT_FOUND

    @pytest.mark.asyncio
    async def test_the_league_read_asks_for_settings_with_the_cookies(self, espn_http):
        espn_http.queue.append((200, {"id": 1111111, "settings": {"isPublic": False}}))
        assert await EspnService.fetch_league_settings("AEB", SWID, 2027, 1111111) == {"isPublic": False}
        request = espn_http.calls[0]
        assert request.url.path.endswith("/seasons/2027/segments/0/leagues/1111111")
        assert request.url.params["view"] == "mSettings" and "espn_s2=AEB" in request.headers["cookie"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_league_read_is_provider_auth(self, espn_http, status):
        espn_http.queue.append((status, {}))
        with pytest.raises(ProviderAuthError):
            await EspnService.fetch_league_settings("AEB", SWID, 2027, 1111111)


FINGERPRINT = ("ciphertext-that-was-checked", 1)


@pytest.fixture
def service(monkeypatch, keys, espn):
    """ConnectionService with the DB replaced by recorders and ESPN by `espn`,
    whose first league is private -- so a pair is accepted unless a test says not.
    Adoption links one team (15) whenever it runs; a verdict is recorded unless
    `recorded` is set False (the row's cookies changed during the check)."""
    state = SimpleNamespace(stored=[], checked=[], fingerprints=[], recorded=True, adopted=[], espn=espn)
    espn.answers = {1111111: {"isPublic": False}}

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def store(user_id, espn_s2, swid, *, verified=True):
        state.stored.append((user_id, espn_s2, swid, verified))
        return 21, True

    def adopt(user_id, connection_id, account_teams):
        state.adopted.append((user_id, connection_id, [t.team_name for t in account_teams]))
        return [15]

    def mark_checked(connection_id, ok, *, fingerprint):
        state.checked.append((connection_id, ok))
        state.fingerprints.append(fingerprint)
        return state.recorded

    def load(user_id, connection_id, provider=None):
        return dict(STORED) if (user_id, connection_id) == (10, 21) else None

    monkeypatch.setattr(cs, "run_db", direct_run_db)
    monkeypatch.setattr(credential_service, "store_espn_cookies", store)
    monkeypatch.setattr(credential_service, "mark_checked", mark_checked)
    monkeypatch.setattr(credential_service, "load_provider_tokens", load)
    monkeypatch.setattr(credential_service, "load_with_fingerprint",
                        lambda uid, cid, provider=None: (load(uid, cid), FINGERPRINT) if load(uid, cid) else None)
    monkeypatch.setattr(cs, "_views", lambda user_id, connection_id=None: [_view(connection_id or 21)])
    monkeypatch.setattr(cs, "_tracked_espn_teams", lambda user_id: [])
    monkeypatch.setattr(cs, "_adopt_account_teams", adopt)
    return state


def _refuse(espn, how):
    if how == "league":
        espn.answers = {1111111: ProviderAuthError("espn")}
    elif how == "anon":
        espn.fan = {**FAN, "anon": True}
    else:
        espn.fan_error = BadRequestError(ESPN_ACCOUNT_NOT_FOUND, "unknown SWID")


ACCOUNT_TEAMS = ["Charlie", "Alpha", "Bravo", "Delta"]


@pytest.mark.unit
class TestConnectEspn:
    @pytest.mark.asyncio
    async def test_confirmed_then_stored_normalized(self, service):
        resp = await cs.ConnectionService.connect_espn(10, " AEB-new \n", SWID.lower())
        assert service.espn.fan_calls == [("AEB-new", SWID)]
        assert service.stored == [(10, "AEB-new", SWID, True)]
        assert resp.created is True and resp.data.id == 21

    @pytest.mark.asyncio
    async def test_an_accepted_pair_takes_over_the_accounts_teams(self, service):
        """Teams saved before the connection existed would otherwise be left out."""
        resp = await cs.ConnectionService.connect_espn(10, "AEB", SWID)
        assert service.adopted == [(10, 21, ACCOUNT_TEAMS)]
        assert "linked 1 team you already track" in resp.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["league", "anon"])
    async def test_a_refused_pair_is_never_stored(self, service, how):
        _refuse(service.espn, how)
        with pytest.raises(ProviderAuthError) as exc:
            await cs.ConnectionService.connect_espn(10, "AEB-bad", SWID)
        assert not service.stored and not service.adopted
        assert "season" not in exc.value.message, "the league-read wording does not apply to an account check"

    @pytest.mark.asyncio
    async def test_an_unknown_swid_is_never_stored(self, service):
        _refuse(service.espn, "unknown_swid")
        with pytest.raises(BadRequestError):
            await cs.ConnectionService.connect_espn(10, "AEB", SWID)
        assert not service.stored

    @pytest.mark.asyncio
    async def test_an_unconfirmable_pair_is_stored_unverified_and_takes_over_nothing(self, service):
        """Only public leagues to read: stored, its status left unknown, and the message says why."""
        service.espn.answers = {}
        resp = await cs.ConnectionService.connect_espn(10, "AEB", SWID)
        assert service.stored == [(10, "AEB", SWID, False)]
        assert not service.adopted, "teams keep their own cookies until ESPN has accepted the new ones"
        assert "couldn't confirm" in resp.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("espn_s2, swid", [("", SWID), ("AEB", "{}"), ("  ", "  ")])
    async def test_both_cookies_are_required(self, service, espn_s2, swid):
        with pytest.raises(BadRequestError) as exc:
            await cs.ConnectionService.connect_espn(10, espn_s2, swid)
        assert exc.value.error_code == "ESPN_COOKIES_REQUIRED" and not service.espn.fan_calls

    @pytest.mark.asyncio
    async def test_refused_without_a_credential_store(self, service, monkeypatch):
        from core.settings import settings

        monkeypatch.setattr(settings, "credential_keys", "", raising=False)
        crypto.reset_cache()
        with pytest.raises(ServiceUnavailableError):
            await cs.ConnectionService.connect_espn(10, "AEB", SWID)
        assert not service.espn.fan_calls


@pytest.mark.unit
class TestVerify:
    @pytest.mark.asyncio
    async def test_accepted_cookies_are_recorded_and_take_over_the_accounts_teams(self, service):
        resp = await cs.ConnectionService.verify(10, 21)
        assert service.espn.fan_calls == [("AEB-STORED", SWID)] and service.checked == [(21, True)]
        assert service.fingerprints == [FINGERPRINT], "the verdict names the cookies it was reached with"
        assert service.adopted == [(10, 21, ACCOUNT_TEAMS)]
        assert "linked 1 team you already track" in resp.message

    @pytest.mark.asyncio
    async def test_a_verdict_on_cookies_replaced_mid_check_is_dropped(self, service):
        """New cookies were saved while ESPN answered: the verdict is not theirs."""
        service.recorded = False
        resp = await cs.ConnectionService.verify(10, 21)
        assert not service.adopted
        assert "replaced while ESPN was checking" in resp.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("how", ["league", "anon", "unknown_swid"])
    async def test_a_refusal_is_the_answer_not_an_error(self, service, how):
        _refuse(service.espn, how)
        resp = await cs.ConnectionService.verify(10, 21)
        assert service.checked == [(21, False)] and resp.status == "success"
        assert not service.adopted

    @pytest.mark.asyncio
    async def test_an_unconfirmable_pair_records_nothing(self, service):
        service.espn.answers = {}
        resp = await cs.ConnectionService.verify(10, 21)
        assert not service.checked and not service.adopted and "couldn't confirm" in resp.message

    @pytest.mark.asyncio
    async def test_an_outage_records_nothing(self, service):
        service.espn.answers = {1111111: ProviderError("espn", "ESPN isn't responding")}
        with pytest.raises(ProviderError):
            await cs.ConnectionService.verify(10, 21)
        assert not service.checked

    @pytest.mark.asyncio
    async def test_someone_elses_connection_is_not_found(self, service):
        with pytest.raises(NotFoundError):
            await cs.ConnectionService.verify(99, 21)
        assert not service.espn.fan_calls


@pytest.mark.unit
class TestEspnTeams:
    @pytest.mark.asyncio
    async def test_lists_the_accounts_teams_with_the_stored_cookies(self, service, monkeypatch):
        monkeypatch.setattr(cs, "_tracked_espn_teams",
                            lambda user_id: [(7, {"league_id": 1111111, "year": 2027, "team_name": "Alpha"})])
        resp = await cs.ConnectionService.espn_teams(10, 21)
        assert service.espn.fan_calls == [("AEB-STORED", SWID)]
        assert [(t.team_name, t.tracked_team_id) for t in resp.data] == [
            ("Charlie", None), ("Alpha", 7), ("Bravo", None), ("Delta", None)]

    @pytest.mark.asyncio
    async def test_someone_elses_connection_is_not_found(self, service):
        with pytest.raises(NotFoundError):
            await cs.ConnectionService.espn_teams(99, 21)
        assert not service.espn.fan_calls
