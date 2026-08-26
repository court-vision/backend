"""
`core.telemetry.scrub_request` (Sentry `before_send`) never lets cookies,
bearer tokens, API keys, ESPN/Yahoo credentials or emails leave the process.
"""

from types import SimpleNamespace

import pytest
import sentry_sdk

from core.telemetry import FILTERED, init_sentry, scrub_request, scrub_string


def _event():
    return {
        "event_id": "abc",
        "level": "error",
        "request": {
            "url": "https://api.courtvision.dev/v1/internal/teams/view?team_id=1",
            "method": "GET",
            "cookies": {"espn_s2": "AEB...", "SWID": "{123}"},
            "headers": {
                "Authorization": "Bearer eyJ...",
                "X-API-Key": "cv_abc",
                "Cookie": "espn_s2=AEB",
                "User-Agent": "Mozilla/5.0",
                "X-Correlation-ID": "cid-1",
            },
            "query_string": "team_id=1&espn_s2=AEB123",
            "env": {"REMOTE_ADDR": "203.0.113.9", "SERVER_NAME": "api"},
            "data": {"league_info": {"league_id": 5, "espn_s2": "AEB", "swid": "{1}"}},
        },
        "user": {"id": "42", "email": "jane@example.com"},
        "extra": {
            "yahoo_refresh_token": "r-1",
            "yahoo_access_token": "a-1",
            "team_id": 7,
            "url": "https://fantasysports.yahooapis.com/x?access_token=abc&format=json",
        },
        "breadcrumbs": {"values": [{"category": "log", "data": {"swid": "{1}", "player_id": 3}}]},
        "exception": {
            "values": [
                {
                    "type": "HTTPError",
                    "value": "403 for https://lm-api.espn.com/x?espn_s2=SECRET&swid={S}",
                    "stacktrace": {"frames": [{"vars": {"password": "hunter2", "limit": 20}}]},
                }
            ]
        },
        "tags": {"correlation_id": "cid-1"},
    }


@pytest.mark.unit
def test_cookies_are_dropped_and_sensitive_headers_filtered():
    event = scrub_request(_event(), {})
    request = event["request"]
    assert "cookies" not in request
    assert request["headers"]["Authorization"] == FILTERED
    assert request["headers"]["X-API-Key"] == FILTERED
    assert request["headers"]["Cookie"] == FILTERED
    assert request["headers"]["User-Agent"] == "Mozilla/5.0"
    assert request["headers"]["X-Correlation-ID"] == "cid-1"
    assert "REMOTE_ADDR" not in request["env"]


@pytest.mark.unit
def test_credentials_are_filtered_wherever_they_appear():
    event = scrub_request(_event(), {})
    assert event["request"]["data"]["league_info"] == {"league_id": 5, "espn_s2": FILTERED, "swid": FILTERED}
    assert event["user"] == {"id": "42", "email": FILTERED}
    assert event["extra"]["yahoo_refresh_token"] == FILTERED
    assert event["extra"]["yahoo_access_token"] == FILTERED
    assert event["extra"]["team_id"] == 7
    assert event["breadcrumbs"]["values"][0]["data"] == {"swid": FILTERED, "player_id": 3}
    frame_vars = event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars == {"password": FILTERED, "limit": 20}
    assert event["tags"]["correlation_id"] == "cid-1"


@pytest.mark.unit
def test_secrets_inside_strings_are_redacted():
    event = scrub_request(_event(), {})
    assert "SECRET" not in event["exception"]["values"][0]["value"]
    assert "{S}" not in event["exception"]["values"][0]["value"]
    assert "403 for https://lm-api.espn.com/x?" in event["exception"]["values"][0]["value"]
    assert event["request"]["query_string"] == f"team_id=1&espn_s2={FILTERED}"
    assert event["extra"]["url"] == f"https://fantasysports.yahooapis.com/x?access_token={FILTERED}&format=json"


@pytest.mark.unit
def test_scrub_string_leaves_ordinary_text_alone():
    assert scrub_string("week=3&team_id=7") == "week=3&team_id=7"
    assert scrub_string("No matchup this week") == "No matchup this week"


@pytest.mark.unit
def test_init_sentry_is_a_noop_without_a_dsn():
    settings = SimpleNamespace(
        sentry_dsn=None,
        sentry_environment="development",
        railway_git_commit_sha=None,
        sentry_traces_sample_rate=0.0,
        service_name="court-vision-api",
    )
    assert init_sentry(settings) is False
    assert sentry_sdk.is_initialized() is False
