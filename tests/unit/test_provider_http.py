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
    await started.wait()
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
    await asyncio.gather(*(
        provider_http.provider_get("espn", URL, expect_key="teams") for _ in range(10)
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
