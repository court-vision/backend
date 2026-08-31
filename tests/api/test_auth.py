"""
Authentication negative tests.

Verifies that:
- Internal routes reject requests with no Authorization header (401 AUTH_REQUIRED)
- Internal routes reject malformed tokens (401 INVALID_TOKEN)
- Public routes do NOT require authentication
"""

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import make_test_app


@pytest.fixture
def unauthed_app():
    """Test app with NO auth override — real Clerk auth runs (and will reject fake tokens)."""
    app = make_test_app()
    app.dependency_overrides.clear()
    return app


@pytest.fixture
def unauthed_client(unauthed_app):
    return TestClient(unauthed_app, raise_server_exceptions=False)


# ---- Internal routes require auth ----

@pytest.mark.api
@pytest.mark.parametrize("path", [
    "/v1/internal/teams/",
    "/v1/internal/api-keys/",
    "/v1/internal/sqlmate/users/get_tables",
])
def test_internal_routes_reject_no_token(unauthed_client, path):
    """A missing bearer is 401 AUTH_REQUIRED in the standard envelope (not FastAPI's 403)."""
    res = unauthed_client.get(path, headers={"X-Correlation-ID": "t-auth"})
    assert res.status_code == 401
    body = res.json()
    assert body["status"] == "authentication_error"
    assert body["error_code"] == "AUTH_REQUIRED"
    assert body["data"]["correlation_id"] == "t-auth"
    assert res.headers["X-Correlation-ID"] == "t-auth"
    assert res.headers["X-Error-Code"] == "AUTH_REQUIRED"


@pytest.mark.api
@pytest.mark.parametrize("path", [
    "/v1/internal/espn/validate_league",
    "/v1/internal/streamers/find",
])
def test_internal_post_routes_reject_no_token(unauthed_client, path):
    """POST routes that take raw league credentials must still require a signed-in user."""
    res = unauthed_client.post(path, json={})
    assert res.status_code == 401
    assert res.json()["error_code"] == "AUTH_REQUIRED"


@pytest.mark.api
@pytest.mark.parametrize("path", [
    "/v1/internal/teams/",
    "/v1/internal/api-keys/",
])
def test_internal_routes_reject_invalid_token(unauthed_client, path):
    """Internal routes must return 401 INVALID_TOKEN when the token is not a real Clerk JWT."""
    res = unauthed_client.get(path, headers={"Authorization": "Bearer not-a-real-jwt"})
    assert res.status_code == 401
    body = res.json()
    assert body["status"] == "authentication_error"
    assert body["error_code"] == "INVALID_TOKEN"


# ---- Public routes do NOT require auth ----

@pytest.mark.api
def test_public_rankings_accessible_without_auth(monkeypatch):
    """Public rankings endpoint must work without any auth header."""
    from services import rankings_service
    from schemas.common import ApiStatus
    from schemas.rankings import RankingsResp

    async def fake_get_rankings(**kwargs):
        return RankingsResp(status=ApiStatus.SUCCESS, message="ok", data=[])

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))

    # Use app with no auth override — public routes should still work
    app = make_test_app()
    app.dependency_overrides.clear()
    client = TestClient(app)

    res = client.get("/v1/rankings/")
    assert res.status_code == 200
