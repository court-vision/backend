"""
API test fixtures.

Provides a test FastAPI app that:
- Skips lifespan (no init_db / close_db)
- Overrides Clerk auth with a fixed fake user payload
- Has rate limiter attached (required by @limiter.limit decorators)
- Installs the real exception handlers + CORS (`setup_middleware`) and the
  `RequestContextMiddleware`, so responses carry the envelope and headers
  production does
- Does NOT add DatabaseMiddleware — API tests monkeypatch services instead

Clients use `raise_server_exceptions=False` so an unhandled error is asserted
as the 500 envelope rather than re-raised into the test.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from core.clerk_auth import get_current_user, verify_clerk_token
from core.correlation_middleware import RequestContextMiddleware
from core.middleware import setup_middleware
from core.rate_limit import limiter, rate_limit_exceeded_handler

# Fixed fake user for all authenticated test requests
FAKE_USER = {
    "clerk_user_id": "user_test_123",
    "email": "test@courtvision.dev",
}


@pytest.fixture(autouse=True)
def execute_mocked_db_operations_without_postgres(monkeypatch):
    """API tests mock repositories/services and intentionally skip Postgres.

    Preserve the async scheduling boundary while replacing only the runtime's
    connection wrapper. Dedicated DB-runtime tests exercise the real executor
    admission/cancellation behavior.
    """
    from api import deps
    from api.v1.internal import lineups
    from core import health

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(deps, "run_db", direct_run_db)
    monkeypatch.setattr(lineups, "run_db", direct_run_db)
    monkeypatch.setattr(health, "run_db", direct_run_db)


def make_test_app() -> FastAPI:
    """
    Build a FastAPI app for API tests.

    Wires routers and middleware identically to main.py but skips the
    lifespan handler (no DB init) and the DatabaseMiddleware. Auth is
    overridden via dependency_overrides so Clerk JWT validation is bypassed.
    """
    from fastapi import APIRouter
    from api.v1.internal import (
        users, teams, lineups, espn, yahoo,
        matchups, streamers, notifications, api_keys,
    )
    from api.v1.public import (
        rankings, players, games,
        teams as public_teams,
        ownership, analytics, schedule,
        live as live_public, playoffs,
    )

    app = FastAPI(title="Court Vision API (test)")

    # Rate limiter must be on app.state for @limiter.limit decorators to work
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Same stack as main.py minus the DatabaseMiddleware
    app.add_middleware(RequestContextMiddleware)
    setup_middleware(app)

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
    api_v1_public.include_router(playoffs.router)
    app.include_router(api_v1_public)

    # Internal routes
    api_v1_internal = APIRouter(prefix="/v1/internal")
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
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def authed_client(app):
    """
    TestClient for internal routes.

    The Authorization header satisfies any middleware that checks for its
    presence, though the token value is irrelevant — auth is overridden.
    """
    return TestClient(
        app,
        headers={"Authorization": "Bearer fake-jwt-token"},
        raise_server_exceptions=False,
    )
