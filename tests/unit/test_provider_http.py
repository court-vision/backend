"""
services.providers.http: every provider outcome becomes one typed AppError with a
real HTTP status, GETs retry exactly once on a 5xx / network error, POSTs never
retry, and each failed call logs one `provider_call_failed` line.

`requests.request` (what core.resilience calls) is stubbed; tenacity's sleep between
attempts is a no-op.
"""

import json
import time
from types import SimpleNamespace

import pytest
import requests
from structlog.testing import capture_logs

from core import resilience
from core.errors import BadRequestError, ProviderAuthError, ProviderError, ProviderTimeout
from core.settings import settings
from services.providers import http as provider_http
from services.providers.http import provider_get, provider_label, provider_post

URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027/segments/0/leagues/555?view=mTeam"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


def _response(status: int, body=b"", headers=None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    response._content = body
    response.headers.update(headers or {})
    response.url = URL
    return response


@pytest.fixture
def http(monkeypatch):
    """`queue` holds the responses (or exceptions) to hand out; `calls` records each attempt."""
    state = SimpleNamespace(queue=[], calls=[])

    def fake_request(method, url, **kwargs):
        state.calls.append((method, url, kwargs))
        nxt = state.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(resilience.requests, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)   # tenacity's nap between attempts
    return state


def _failures(logs):
    return [e for e in logs if e["event"] == "provider_call_failed"]


@pytest.mark.unit
def test_success_returns_the_json_body_and_passes_the_request_through(http):
    http.queue = [_response(200, {"teams": [{"name": "My Team"}]})]

    body = provider_get("espn", URL, params={"view": ["mTeam"]}, cookies={"espn_s2": "s2", "SWID": "{w}"},
                        headers={"x-fantasy-filter": "{}"}, expect_key="teams")

    assert body == {"teams": [{"name": "My Team"}]}
    (method, url, kwargs), = http.calls
    assert method == "GET" and url == URL
    assert kwargs["params"] == {"view": ["mTeam"]} and kwargs["cookies"] == {"espn_s2": "s2", "SWID": "{w}"}
    assert kwargs["headers"] == {"x-fantasy-filter": "{}"} and kwargs["timeout"] == settings.http_timeout


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_a_provider_auth_error(http, status):
    http.queue = [_response(status, "<html>login</html>")]

    with capture_logs() as logs, pytest.raises(ProviderAuthError) as excinfo:
        provider_get("yahoo", URL)

    err = excinfo.value
    assert err.status_code == 403 and err.error_code == "PROVIDER_AUTH_EXPIRED" and err.data["provider"] == "yahoo"
    assert len(http.calls) == 1                                   # a 4xx is never retried
    failed = _failures(logs)
    assert len(failed) == 1 and failed[0]["provider"] == "yahoo" and failed[0]["status"] == status
    assert isinstance(failed[0]["elapsed_ms"], int) and failed[0]["log_level"] == "warning"
    assert failed[0]["error_code"] == "PROVIDER_AUTH_EXPIRED"
    assert "?" not in failed[0]["url"]                            # query strings never reach the logs


@pytest.mark.unit
def test_a_404_is_league_not_found(http):
    http.queue = [_response(404, {"message": "not found"})]

    with pytest.raises(BadRequestError) as excinfo:
        provider_get("espn", URL, expect_key="teams")

    assert excinfo.value.status_code == 400 and excinfo.value.error_code == "LEAGUE_NOT_FOUND"
    assert "ESPN" in excinfo.value.message and "not found" in excinfo.value.message
    assert len(http.calls) == 1


@pytest.mark.unit
def test_a_5xx_is_retried_once_then_succeeds(http):
    http.queue = [_response(503, "gateway"), _response(200, {"teams": []})]

    with capture_logs() as logs:
        body = provider_get("espn", URL, expect_key="teams")

    assert body == {"teams": []} and len(http.calls) == 2
    assert _failures(logs) == []                                  # the call succeeded: nothing to alert on


@pytest.mark.unit
def test_two_5xx_are_a_provider_error_after_two_attempts(http):
    http.queue = [_response(500), _response(502)]

    with capture_logs() as logs, pytest.raises(ProviderError) as excinfo:
        provider_get("espn", URL)

    assert excinfo.value.status_code == 502 and excinfo.value.error_code == "PROVIDER_UNAVAILABLE"
    assert excinfo.value.data["provider"] == "espn" and len(http.calls) == 2
    failed = _failures(logs)
    assert len(failed) == 1 and failed[0]["status"] == 502 and failed[0]["error_code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.unit
def test_timeouts_retry_once_then_map_to_provider_timeout(http):
    http.queue = [requests.exceptions.Timeout("slow"), requests.exceptions.ConnectionError("refused")]

    with capture_logs() as logs, pytest.raises(ProviderTimeout) as excinfo:
        provider_get("espn", URL)

    assert excinfo.value.status_code == 504 and excinfo.value.error_code == "PROVIDER_TIMEOUT"
    assert excinfo.value.data["provider"] == "espn" and len(http.calls) == 2
    assert _failures(logs)[0]["status"] is None

    http.calls.clear()
    http.queue = [requests.exceptions.ConnectionError("blip"), _response(200, {"players": []})]
    assert provider_get("espn", URL, expect_key="players") == {"players": []}
    assert len(http.calls) == 2


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 429])
def test_other_client_errors_are_provider_errors_without_retry(http, status):
    http.queue = [_response(status, "nope", headers={"Retry-After": "7"})]

    with pytest.raises(ProviderError) as excinfo:
        provider_get("espn", URL)

    assert excinfo.value.status_code == 502 and excinfo.value.error_code == "PROVIDER_UNAVAILABLE"
    assert len(http.calls) == 1
    if status == 429:
        assert excinfo.value.data["retry_after"] == 7


@pytest.mark.unit
def test_unreadable_or_unexpected_bodies_are_bad_responses(http):
    http.queue = [_response(200, "<html>maintenance</html>")]
    with capture_logs() as logs, pytest.raises(ProviderError) as excinfo:
        provider_get("espn", URL, expect_key="teams")
    assert excinfo.value.error_code == "PROVIDER_BAD_RESPONSE" and excinfo.value.status_code == 502
    assert _failures(logs)[0]["body_preview"].startswith("<html>")
    assert "<html>" not in excinfo.value.message                  # provider text never reaches the client

    http.queue = [_response(200, {"error": "no teams here"})]
    with pytest.raises(ProviderError) as excinfo:
        provider_get("espn", URL, expect_key="teams")
    assert excinfo.value.error_code == "PROVIDER_BAD_RESPONSE"

    http.queue = [_response(200, {"error": "fine without expect_key"})]
    assert provider_get("espn", URL) == {"error": "fine without expect_key"}


@pytest.mark.unit
def test_post_maps_a_rejected_grant_to_provider_auth_and_never_retries(http):
    http.queue = [_response(400, {"error": "INVALID_REFRESH_TOKEN"})]
    with pytest.raises(ProviderAuthError):
        provider_post("yahoo", TOKEN_URL, data={"grant_type": "refresh_token"},
                      expect_key="access_token", auth_statuses=(400, 401, 403))
    (method, _, kwargs), = http.calls
    assert method == "POST" and kwargs["data"] == {"grant_type": "refresh_token"}

    http.calls.clear()
    http.queue = [_response(503)]
    with pytest.raises(ProviderError):
        provider_post("yahoo", TOKEN_URL, data={})
    assert len(http.calls) == 1                                   # POSTs are not idempotent: no retry

    http.calls.clear()
    http.queue = [_response(404)]
    with pytest.raises(ProviderError):                            # no league to be missing on a token URL
        provider_post("yahoo", TOKEN_URL, data={})

    http.calls.clear()
    http.queue = [_response(200, {"access_token": "new", "refresh_token": "r", "expires_in": 3600})]
    body = provider_post("yahoo", TOKEN_URL, data={}, expect_key="access_token")
    assert body["access_token"] == "new"


@pytest.mark.unit
def test_retry_policy_and_labels():
    assert (provider_http.RETRY_MAX_ATTEMPTS, provider_http.RETRY_BASE_DELAY, provider_http.RETRY_MAX_DELAY) == (2, 0.5, 1)
    assert provider_label("espn") == "ESPN" and provider_label("yahoo") == "Yahoo" and provider_label("nba") == "NBA"
