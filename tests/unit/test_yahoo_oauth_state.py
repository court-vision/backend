"""The OAuth state carries where the callback should send the browser back
to, and only a path on the frontend's own origin gets in."""

import pytest
from pydantic import SecretStr

from core.settings import settings
from services.yahoo.oauth import YahooOAuthService, safe_return_path


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "yahoo_client_id", "client-id")
    monkeypatch.setattr(settings, "yahoo_client_secret", SecretStr("client-secret"))
    monkeypatch.setattr(settings, "clerk_secret_key", SecretStr("clerk-secret"))


@pytest.mark.unit
@pytest.mark.parametrize("path", [
    "/week",
    "/me",
    "/me?add",
    "/week?view=matchup&market=day",
    "/lab/12/recap",
    "/manage-teams?add=1",
    "/a-b_c.d~e/",
])
def test_a_path_on_our_own_origin_is_kept(path):
    assert safe_return_path(path) == path


@pytest.mark.unit
@pytest.mark.parametrize("value", [
    None,
    "",
    "week",                       # relative
    "https://evil.example/week",  # another origin
    "//evil.example/week",        # protocol-relative
    "/\\evil.example",            # backslash host trick
    "/week#frag",
    "/week?q=<script>",
    "/../week",
    "/me/../admin",
    "/week?x=1 ",                 # whitespace
    "/" + "a" * 200,              # too long
])
def test_anything_else_is_dropped(value):
    assert safe_return_path(value) is None


@pytest.mark.unit
def test_the_state_round_trips_the_return_path(configured):
    _url, state = YahooOAuthService.get_auth_url("user_1", "/week?view=matchup")

    payload = YahooOAuthService.validate_state(state)

    assert payload["user_id"] == "user_1"
    assert payload["return_to"] == "/week?view=matchup"


@pytest.mark.unit
def test_a_bad_return_path_never_enters_the_state(configured):
    _url, state = YahooOAuthService.get_auth_url("user_1", "https://evil.example/")

    payload = YahooOAuthService.validate_state(state)

    assert "return_to" not in payload


@pytest.mark.unit
def test_no_return_path_is_the_old_state(configured):
    _url, state = YahooOAuthService.get_auth_url("user_1")

    assert "return_to" not in YahooOAuthService.validate_state(state)
