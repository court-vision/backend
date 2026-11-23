"""Async provider boundary: mappings, retries, capacity, and credential isolation."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from core.errors import BadRequestError, ProviderAuthError, ProviderError, ProviderTimeout
from services.providers import http as provider_http

URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/555"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


@pytest_asyncio.fixture
async def http(monkeypatch):
    state = SimpleNamespace(queue=[], calls=[])

    async def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        nxt = state.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        status, body, headers = nxt
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=body or "", headers=headers)

    await provider_http.stop_provider_runtime()
    monkeypatch.setattr(provider_http, "RETRY_BASE_DELAY", 0)
    for provider in ("espn", "yahoo", "nba"):
        provider_http._clients[provider] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider_http._capacity[provider] = asyncio.Semaphore(provider_http._limit_for(provider))
    yield state
    await provider_http.stop_provider_runtime()


def response(status: int, body=None, headers=None):
    return status, body, headers or {}


def failures(logs):
    return [event for event in logs if event["event"] == "provider_call_failed"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_swid_in_the_path_stays_out_of_the_failure_log(http):
    """ESPN's fan API takes the account's SWID as a path segment, and a failed
    call logs its URL."""
    http.queue.extend([response(500, "down"), response(500, "down")])
    with capture_logs() as logs, pytest.raises(ProviderError):
        await provider_http.provider_get(
            "espn", "https://fan.api.espn.com/apis/v2/fans/{3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6}"
        )
    (event,) = failures(logs)
    assert event["url"] == "https://fan.api.espn.com/apis/v2/fans/{SWID}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_swid_in_an_unreadable_body_stays_out_of_the_failure_log(http):
    """The first characters of a body that is not JSON are logged, and an ESPN
    page can carry a SWID in them."""
    http.queue.append(response(200, "<html>signed in as {3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6}</html>"))
    with capture_logs() as logs, pytest.raises(ProviderError):
        await provider_http.provider_get("espn", URL)
    (event,) = failures(logs)
    assert "3F2A9C1E" not in event["body_preview"]
    assert event["body_preview"].startswith("<html>signed in as ")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_passes_request_and_returns_json(http):
    http.queue = [response(200, {"teams": [{"name": "My Team"}]})]
    body = await provider_http.provider_get(
        "espn", URL,
        params={"view": ["mTeam"]},
        cookies={"espn_s2": "s2", "SWID": "{w}"},
        headers={"x-fantasy-filter": "{}"},
        expect_key="teams",
    )
    assert body == {"teams": [{"name": "My Team"}]}
    request = http.calls[0]
    assert request.method == "GET" and request.url.params.get("view") == "mTeam"
    assert request.headers["cookie"] == "espn_s2=s2; SWID={w}"
    assert request.headers["x-fantasy-filter"] == "{}"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_map_without_retry(http, status):
    http.queue = [response(status, "<html>login</html>")]
    with capture_logs() as logs, pytest.raises(ProviderAuthError) as excinfo:
        await provider_http.provider_get("yahoo", URL)
    assert excinfo.value.status_code == 403
    assert excinfo.value.error_code == "PROVIDER_AUTH_EXPIRED"
    assert len(http.calls) == 1 and failures(logs)[0]["status"] == status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_refusal_logs_the_providers_own_reason(http):
    """Yahoo answers "not authorized for this API" and "token expired" with the
    same 403; only the body tells them apart, so the failure log carries it."""
    http.queue = [response(403, '<yahoo:error><yahoo:description>Please provide valid credentials. '
                                 'OAuth oauth_problem="this application is not authorized"</yahoo:description></yahoo:error>')]
    with capture_logs() as logs, pytest.raises(ProviderAuthError):
        await provider_http.provider_get("yahoo", URL, headers={"Authorization": "Bearer t"})
    assert "not authorized" in failures(logs)[0]["body_preview"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_say_so_when_some_were_sent(http, status):
    """Cookies were sent and refused: the credentials themselves are suspect."""
    http.queue = [response(status, "<html>login</html>")]
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider_http.provider_get(
            "espn", URL, cookies={"espn_s2": "s2", "SWID": "{w}"}, expect_key="teams"
        )
    assert "rejected this league's credentials" in excinfo.value.message


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cookies", [None, {}, {"espn_s2": "", "SWID": ""}])
async def test_missing_credentials_are_named_as_missing(http, cookies):
    """Nothing was sent, so the league is private and nothing was loaded for it.

    Blaming the stored cookies here sends people to re-enter credentials that
    are fine — which is exactly what happened when a stale client posted an
    empty league_info.
    """
    http.queue = [response(401, "<html>login</html>")]
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider_http.provider_get("espn", URL, cookies=cookies, expect_key="teams")
    assert "private and no credentials were sent" in excinfo.value.message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_league_needs_no_credentials(http):
    """A public league answers 200 without cookies and never reaches the branch."""
    http.queue = [response(200, {"teams": [{"name": "My Team"}]})]
    body = await provider_http.provider_get("espn", URL, expect_key="teams")
    assert body == {"teams": [{"name": "My Team"}]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bearer_header_counts_as_credentials(http):
    """Yahoo authenticates with a header, not cookies."""
    http.queue = [response(401, {"error": "invalid_token"})]
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider_http.provider_get(
            "yahoo", URL, headers={"Authorization": "Bearer t"}
        )
    assert "connection expired" in excinfo.value.message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_404_maps_to_league_not_found(http):
    http.queue = [response(404, {"message": "not found"})]
    with pytest.raises(BadRequestError) as excinfo:
        await provider_http.provider_get("espn", URL, expect_key="teams")
    assert excinfo.value.status_code == 400 and excinfo.value.error_code == "LEAGUE_NOT_FOUND"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_retries_network_and_5xx_once(http):
    http.queue = [response(503), response(200, {"teams": []})]
    assert await provider_http.provider_get("espn", URL, expect_key="teams") == {"teams": []}
    assert len(http.calls) == 2

    http.calls.clear()
    http.queue = [httpx.ConnectError("blip"), response(200, {"players": []})]
    assert await provider_http.provider_get("espn", URL, expect_key="players") == {"players": []}
    assert len(http.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_retries_map_to_typed_errors(http):
    http.queue = [response(500), response(502)]
    with pytest.raises(ProviderError) as excinfo:
        await provider_http.provider_get("espn", URL)
    assert excinfo.value.status_code == 502 and len(http.calls) == 2

    http.calls.clear()
    http.queue = [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")]
    with pytest.raises(ProviderTimeout) as excinfo:
        await provider_http.provider_get("espn", URL)
    assert excinfo.value.status_code == 504 and len(http.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429])
async def test_other_client_errors_do_not_retry(http, status):
    http.queue = [response(status, "nope", {"Retry-After": "7"})]
    with pytest.raises(ProviderError) as excinfo:
        await provider_http.provider_get("espn", URL)
    assert len(http.calls) == 1
    if status == 429:
        assert excinfo.value.data["retry_after"] == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_json_and_missing_key_map_to_bad_response(http):
    http.queue = [response(200, "<html>maintenance</html>")]
    with capture_logs() as logs, pytest.raises(ProviderError) as excinfo:
        await provider_http.provider_get("espn", URL, expect_key="teams")
    assert excinfo.value.error_code == "PROVIDER_BAD_RESPONSE"
    assert failures(logs)[0]["body_preview"].startswith("<html>")

    http.queue = [response(200, {"error": "missing"})]
    with pytest.raises(ProviderError):
        await provider_http.provider_get("espn", URL, expect_key="teams")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_never_retries(http):
    http.queue = [response(400, {"error": "INVALID_REFRESH_TOKEN"})]
    with pytest.raises(ProviderAuthError):
        await provider_http.provider_post(
            "yahoo", TOKEN_URL, data={"grant_type": "refresh_token"},
            expect_key="access_token", auth_statuses=(400, 401, 403),
        )
    assert len(http.calls) == 1

    http.calls.clear()
    http.queue = [response(503), response(200, {"access_token": "unused"})]
    with pytest.raises(ProviderError):
        await provider_http.provider_post("yahoo", TOKEN_URL, data={})
    assert len(http.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_credentials_never_cross_requests(http):
    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("cookie"), request.headers.get("authorization")))
        if len(seen) == 2:
            started.set()
        await release.wait()
        return httpx.Response(200, json={"teams": []}, headers={"Set-Cookie": "espn_s2=server; Path=/"})

    await provider_http.stop_provider_runtime()
    client = httpx.AsyncClient(transport=provider_http._NoCookiePersistenceTransport(httpx.MockTransport(handler)))
    provider_http._clients["espn"] = client
    provider_http._capacity["espn"] = asyncio.Semaphore(2)
    tasks = [
        asyncio.create_task(provider_http.provider_get(
            "espn", URL, cookies={"espn_s2": "user-a"},
            headers={"Authorization": "Bearer user-a"}, expect_key="teams",
        )),
        asyncio.create_task(provider_http.provider_get(
            "espn", URL, cookies={"espn_s2": "user-b"},
            headers={"Authorization": "Bearer user-b"}, expect_key="teams",
        )),
    ]
    await asyncio.wait_for(started.wait(), 1)   # coalescing the two would leave it unset
    release.set()
    await asyncio.gather(*tasks)
    assert set(seen) == {
        ("espn_s2=user-a", "Bearer user-a"),
        ("espn_s2=user-b", "Bearer user-b"),
    }
    assert dict(client.cookies) == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_capacity_is_enforced(http):
    active = maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"teams": []})
        finally:
            active -= 1

    await provider_http.stop_provider_runtime()
    provider_http._clients["espn"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider_http._capacity["espn"] = asyncio.Semaphore(2)
    # Ten different reads: ten identical ones would now share one request.
    await asyncio.gather(*(
        provider_http.provider_get("espn", URL, params={"scoringPeriodId": n}, expect_key="teams")
        for n in range(10)
    ))
    assert maximum == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_capacity_timeout_maps_to_504(http, monkeypatch):
    monkeypatch.setattr(provider_http.settings, "provider_queue_timeout_seconds", 0.01)
    provider_http._capacity["espn"] = asyncio.Semaphore(0)
    with capture_logs() as logs, pytest.raises(ProviderTimeout) as excinfo:
        await provider_http.provider_get("espn", URL)
    assert excinfo.value.status_code == 504
    assert any(event["event"] == "provider_capacity_timeout" for event in logs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_retry_uses_an_async_half_second_backoff(http, monkeypatch):
    http.queue = [response(503), response(200, {"teams": []})]
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(provider_http, "RETRY_BASE_DELAY", 0.5)
    monkeypatch.setattr(provider_http.asyncio, "sleep", fake_sleep)
    await provider_http.provider_get("espn", URL, expect_key="teams")
    assert sleeps == [0.5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_clients_have_explicit_lifecycle(monkeypatch):
    await provider_http.stop_provider_runtime()
    created = []

    def make_client(provider):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        ))
        created.append(client)
        return client

    monkeypatch.setattr(provider_http, "_make_client", make_client)
    provider_http.start_provider_runtime()
    assert set(provider_http._clients) == {"espn", "yahoo", "nba"}
    assert len(created) == 3 and all(not client.is_closed for client in created)
    await provider_http.stop_provider_runtime()
    assert all(client.is_closed for client in created)


@pytest.mark.unit
def test_policy_and_labels():
    assert provider_http.RETRY_MAX_ATTEMPTS == 2
    assert provider_http.provider_label("espn") == "ESPN"


# ---- Coalescing identical in-flight GETs (core/inflight.py) ----

COOKIES = {"espn_s2": "s2-a", "SWID": "{A}"}
VIEWS = {"view": ["mTeam", "mRoster", "mMatchup", "mSettings", "mSchedule"]}


@pytest_asyncio.fixture
async def gated(monkeypatch):
    """Every request blocks until `release` is set, so reads overlap the way a
    Week open's do; each answer says which upstream request produced it."""
    state = SimpleNamespace(calls=[], release=asyncio.Event(), status=200, text=None, cancelled=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        call = len(state.calls)
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled += 1
            raise
        if state.status != 200 or state.text is not None:
            return httpx.Response(state.status, text=state.text or "")
        return httpx.Response(200, json={"teams": [{"id": 1, "roster": {"entries": []}}], "call": call})

    await provider_http.stop_provider_runtime()
    monkeypatch.setattr(provider_http, "RETRY_BASE_DELAY", 0)
    monkeypatch.setattr(provider_http.settings, "provider_read_coalescing", True)
    for provider in ("espn", "yahoo", "nba"):
        provider_http._clients[provider] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider_http._capacity[provider] = asyncio.Semaphore(provider_http._limit_for(provider))
    yield state
    # A test that failed mid-way leaves requests parked on the gate; let them
    # finish so the failure is reported instead of hanging the run.
    state.release.set()
    await _settle()
    await provider_http.stop_provider_runtime()


async def _until(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _league_read(**overrides):
    kwargs = {"params": VIEWS, "cookies": COOKIES, "expect_key": "teams", **overrides}
    return asyncio.create_task(provider_http.provider_get("espn", URL, **kwargs))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_identical_reads_share_one_request(gated):
    """The Week open's three matchup routes read the same league at once."""
    tasks = [_league_read() for _ in range(3)]
    await _until(lambda: len(gated.calls) == 1)
    await _settle()
    gated.release.set()
    bodies = await asyncio.gather(*tasks)
    assert len(gated.calls) == 1
    assert bodies[0] == bodies[1] == bodies[2] == {"teams": [{"id": 1, "roster": {"entries": []}}], "call": 1}
    assert provider_http._reads.stats()["in_flight"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_caller_gets_its_own_copy_of_the_payload(gated):
    """Callers build objects from the dict (the league sync reuses the
    validation payload); one caller's edits must never reach another's."""
    tasks = [_league_read() for _ in range(3)]
    await _until(lambda: len(gated.calls) == 1)
    gated.release.set()
    first, second, third = await asyncio.gather(*tasks)
    assert first is not second and first["teams"] is not second["teams"]
    first["teams"][0]["roster"]["entries"].append({"playerId": 99})
    del second["call"]
    assert third == {"teams": [{"id": 1, "roster": {"entries": []}}], "call": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_param_order_does_not_split_a_read(gated):
    tasks = [
        _league_read(params={"view": ["mTeam", "mRoster"], "scoringPeriodId": 4}),
        _league_read(params={"scoringPeriodId": 4, "view": ["mTeam", "mRoster"]}),
    ]
    await _until(lambda: len(gated.calls) == 1)
    await _settle()
    gated.release.set()
    await asyncio.gather(*tasks)
    assert len(gated.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("other", [
    {"cookies": {"espn_s2": "s2-b", "SWID": "{B}"}},                  # another user's cookies
    {"cookies": {"espn_s2": "s2-a", "SWID": "{B}"}},                  # same s2, other SWID
    {"cookies": None},                                                 # no credentials at all
    {"params": {"view": ["mTeam", "mRoster", "mSettings"]}},           # other views
    {"params": {**VIEWS, "scoringPeriodId": 5}},                       # another day
    {"params": {"view": list(reversed(VIEWS["view"]))}},               # a repeated key's order is kept
    {"headers": {"x-fantasy-filter": '{"players":{"limit":50}}'}},     # a header that shapes the answer
    {"timeout": 3.0},
])
async def test_reads_that_differ_in_anything_sent_do_not_share(gated, other):
    tasks = [_league_read(), _league_read(**other)]
    await _until(lambda: len(gated.calls) == 2)
    gated.release.set()
    first, second = await asyncio.gather(*tasks)
    assert {first["call"], second["call"]} == {1, 2}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_request_carries_only_its_own_credentials(gated):
    tasks = [_league_read(), _league_read(cookies={"espn_s2": "s2-b", "SWID": "{B}"}), _league_read()]
    await _until(lambda: len(gated.calls) == 2)
    await _settle()
    gated.release.set()
    await asyncio.gather(*tasks)
    assert sorted(call.headers["cookie"] for call in gated.calls) == ["espn_s2=s2-a; SWID={A}", "espn_s2=s2-b; SWID={B}"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sequential_identical_reads_are_not_cached(gated):
    gated.release.set()
    first = await _league_read()
    second = await _league_read()
    assert (first["call"], second["call"]) == (1, 2)
    assert len(gated.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failure_reaches_every_joiner_through_its_own_mapping(gated):
    """One upstream 404; each caller maps it with its own `not_found`."""
    gated.status = 404
    account_missing = BadRequestError("ESPN_ACCOUNT_NOT_FOUND", "no such account")
    tasks = [_league_read(), _league_read(not_found=account_missing), _league_read()]
    await _until(lambda: len(gated.calls) == 1)
    await _settle()
    gated.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(gated.calls) == 1
    assert [o.error_code for o in outcomes] == ["LEAGUE_NOT_FOUND", "ESPN_ACCOUNT_NOT_FOUND", "LEAGUE_NOT_FOUND"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_outage_is_retried_once_for_everyone_not_once_each(gated):
    gated.status = 503
    tasks = [_league_read() for _ in range(3)]
    await _until(lambda: len(gated.calls) == 1)
    await _settle()
    gated.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(gated.calls) == 2           # one request and its one retry, shared
    assert all(isinstance(o, ProviderError) and o.status_code == 502 for o in outcomes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_caller_arriving_after_a_failed_attempt_sends_its_own_request(gated):
    """The read is sealed before its retry: the callers that joined before the
    first attempt failed share the retry, but one arriving afterwards gets its
    own request and its own two attempts, as before coalescing, rather than a
    seat on the leader's last one."""
    calls, statuses = [], [503, 200, 200]
    gates = [asyncio.Event() for _ in statuses]

    async def handler(request: httpx.Request) -> httpx.Response:
        n = len(calls)
        calls.append(request)
        await gates[n].wait()
        if statuses[n] != 200:
            return httpx.Response(statuses[n], text="")
        return httpx.Response(200, json={"teams": [], "call": n + 1})

    provider_http._clients["espn"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        early = [_league_read() for _ in range(2)]
        await _until(lambda: len(calls) == 1)
        await _settle()
        gates[0].set()                                    # the first attempt fails ...
        await _until(lambda: len(calls) == 2)             # ... and its retry is on the wire
        assert provider_http._reads.stats()["in_flight"] == 0
        late = _league_read()
        await _until(lambda: len(calls) == 3)             # a request of its own
        gates[1].set()
        gates[2].set()
        bodies = await asyncio.gather(*early, late)
    finally:
        for gate in gates:
            gate.set()
    assert [body["call"] for body in bodies] == [2, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unreadable_body_is_a_bad_response_for_every_joiner(gated):
    gated.text = "<html>maintenance</html>"
    tasks = [_league_read() for _ in range(2)]
    await _until(lambda: len(gated.calls) == 1)
    await _settle()
    gated.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert [o.error_code for o in outcomes] == ["PROVIDER_BAD_RESPONSE"] * 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_first_caller_giving_up_does_not_fail_the_others(gated):
    leader = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    joiners = [_league_read() for _ in range(2)]
    await _settle()
    leader.cancel()
    await _settle()
    gated.release.set()
    bodies = await asyncio.gather(*joiners)
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert [body["call"] for body in bodies] == [1, 1]
    assert len(gated.calls) == 1 and gated.cancelled == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_lone_caller_giving_up_still_cancels_its_request(gated):
    """As before coalescing: the request stops and its ESPN slot comes back."""
    capacity = provider_http._capacity["espn"]
    free = capacity._value
    task = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: gated.cancelled == 1)
    await _until(lambda: capacity._value == free)
    assert provider_http._reads.stats()["in_flight"] == 0


def _fake_writer(monkeypatch, answer):
    """services.fantasy_writer_client over a MockTransport that answers at once,
    with a fresh runtime so its semaphore belongs to this test's loop."""
    from services import fantasy_writer_client as fw

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer(request)

    monkeypatch.setattr(fw, "_client", None)
    monkeypatch.setattr(fw, "_capacity", None)
    monkeypatch.setattr(fw, "client_factory",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://writer.test"))
    return fw, requests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_read_after_a_write_never_joins_one_from_before_it(gated, monkeypatch):
    """The verify re-read after a lineup move, and the board the client then
    refetches, must not be answered by a read that began before ESPN took the
    write -- even though that read is still in flight."""
    fw, writes = _fake_writer(monkeypatch, lambda request: httpx.Response(200, json={"ok": True, "http_status": 200}))
    before = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    await fw.apply_lineup({"moves": [{"player_id": 1, "from_slot_id": 12, "to_slot_id": 0}]})
    after = [_league_read() for _ in range(2)]
    await _until(lambda: len(gated.calls) == 2)
    await _settle()
    gated.release.set()
    stale, *fresh = await asyncio.gather(before, *after)
    assert len(writes) == 1
    assert stale["call"] == 1
    assert [body["call"] for body in fresh] == [2, 2]     # they still share with each other


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_write_fences_reads_too(gated, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("writer slow")       # ambiguous: ESPN may have taken it

    fw, _ = _fake_writer(monkeypatch, timeout)
    before = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    with pytest.raises(fw.FantasyWriterUnavailable):
        await fw.apply_transaction({"add_player_id": 7})
    after = _league_read()
    await _until(lambda: len(gated.calls) == 2)
    gated.release.set()
    stale, fresh = await asyncio.gather(before, after)
    assert (stale["call"], fresh["call"]) == (1, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reads_during_a_write_join_neither_side_of_it(gated):
    from core.inflight import provider_write

    before = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    with provider_write():
        during = [_league_read() for _ in range(2)]
        await _until(lambda: len(gated.calls) == 2)
        await _settle()
    after = _league_read()
    await _until(lambda: len(gated.calls) == 3)
    gated.release.set()
    results = await asyncio.gather(before, *during, after)
    assert [body["call"] for body in results] == [1, 2, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_provider_post_fences_reads(gated):
    """Yahoo's token exchange is the only POST today; it is fenced like a write."""
    provider_http._clients["yahoo"] = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"access_token": "unused"})
    ))
    before = _league_read()
    await _until(lambda: len(gated.calls) == 1)
    await provider_http.provider_post("yahoo", TOKEN_URL, data={})
    after = _league_read()                            # `before` is still in flight
    await _until(lambda: len(gated.calls) == 2)
    gated.release.set()
    stale, fresh = await asyncio.gather(before, after)
    assert (stale["call"], fresh["call"]) == (1, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_kill_switch_sends_every_read_on_its_own(gated, monkeypatch):
    monkeypatch.setattr(provider_http.settings, "provider_read_coalescing", False)
    tasks = [_league_read() for _ in range(3)]
    await _until(lambda: len(gated.calls) == 3)
    gated.release.set()
    bodies = await asyncio.gather(*tasks)
    assert sorted(body["call"] for body in bodies) == [1, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_coalesced_read_is_logged_without_query_or_credentials(gated):
    with capture_logs() as logs:
        tasks = [_league_read(params={**VIEWS, "scoringPeriodId": 3}) for _ in range(4)]
        await _until(lambda: len(gated.calls) == 1)
        await _settle()
        gated.release.set()
        await asyncio.gather(*tasks)
        gated.calls.clear()
        await _league_read()                          # alone: nothing to report
    (event,) = [e for e in logs if e["event"] == "provider_get_coalesced"]
    assert event["joined"] == 3 and event["provider"] == "espn"
    assert event["url"] == URL                        # no query string
    assert "s2-a" not in repr(event) and "{A}" not in repr(event)
