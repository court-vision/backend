"""
API test fixtures.

Provides a test FastAPI app that:
- Skips lifespan (no init_db / close_db)
- Overrides Clerk auth with a fixed fake user payload
- Has rate limiter attached (required by @limiter.limit decorators)
- Does NOT add DatabaseMiddleware — API tests monkeypatch services instead
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from core.clerk_auth import get_current_user, verify_clerk_token
from core.rate_limit import limiter, rate_limit_exceeded_handler

# Fixed fake user for all authenticated test requests
FAKE_USER = {
    "clerk_user_id": "user_test_123",
    "email": "test@courtvision.dev",
}


def make_test_app() -> FastAPI:
    """
    Build a FastAPI app for API tests.

    Wires routers identically to main.py but skips the lifespan handler
    (no DB init). Auth is overridden via dependency_overrides so Clerk
    JWT validation is bypassed entirely.
    """
    from fastapi import APIRouter
    from api.v1.internal import (
        auth, users, teams, lineups, espn, yahoo,
        matchups, streamers, notifications, api_keys,
    )
    from api.v1.public import (
        rankings, players, games,
        teams as public_teams,
        ownership, analytics, schedule,
        live as live_public,
    )

    app = FastAPI(title="Court Vision API (test)")

    # Rate limiter must be on app.state for @limiter.limit decorators to work
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Override auth — replaces both verify_clerk_token and get_current_user
    # with a no-arg lambda so no HTTPBearer / JWKS validation runs
    app.dependency_overrides[verify_clerk_token] = lambda: FAKE_USER
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER

    # Public routes
    api_v1_public = APIRouter(prefix="/v1")
    api_v1_public.include_router(rankings.router)
    api_v1_public.include_router(players.router)
    api_v1_public.include_router(games.router)
    api_v1_public.include_router(public_teams.router)
    api_v1_public.include_router(ownership.router)
    api_v1_public.include_router(analytics.router)
    api_v1_public.include_router(schedule.router)
    api_v1_public.include_router(live_public.router)
    app.include_router(api_v1_public)

    # Internal routes
    api_v1_internal = APIRouter(prefix="/v1/internal")
    api_v1_internal.include_router(auth.router)
    api_v1_internal.include_router(users.router)
    api_v1_internal.include_router(teams.router)
    api_v1_internal.include_router(lineups.router)
    api_v1_internal.include_router(espn.router)
    api_v1_internal.include_router(yahoo.router)
    api_v1_internal.include_router(matchups.router)
    api_v1_internal.include_router(streamers.router)
    api_v1_internal.include_router(notifications.router)
    api_v1_internal.include_router(api_keys.router)
    app.include_router(api_v1_internal)

    return app


@pytest.fixture
def app():
    """Test FastAPI app with auth overrides and no DB middleware."""
    return make_test_app()


@pytest.fixture
def client(app):
    """TestClient for public routes (no auth header needed)."""
    return TestClient(app)


@pytest.fixture
def authed_client(app):
    """
    TestClient for internal routes.

    The Authorization header satisfies any middleware that checks for its
    presence, though the token value is irrelevant — auth is overridden.
    """
    return TestClient(app, headers={"Authorization": "Bearer fake-jwt-token"})
