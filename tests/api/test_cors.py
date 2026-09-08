"""
CORS: who may call this API from a browser.

The allowlist is exact-match, which is why Vercel previews needed a regex --
every preview deployment gets a hostname of its own. The refusals below matter
as much as the acceptances: `allow_credentials=True` means anything that gets
past this can make authenticated requests.
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from tests.api.conftest import make_test_app

PREVIEW = "https://courtvision-git-draft-lab-mock-format-jameslk3s-projects.vercel.app"
DEPLOYMENT = "https://courtvision-9zpn9jyxr-jameslk3s-projects.vercel.app"


@pytest.fixture
def client():
    app = make_test_app()
    router = APIRouter()

    @router.get("/v1/cors-probe")
    async def probe():
        return {"ok": True}

    app.include_router(router)
    return TestClient(app)


def _preflight(client, origin):
    return client.options(
        "/v1/cors-probe",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


@pytest.mark.api
@pytest.mark.parametrize("origin", [
    "https://courtvision.dev",
    "https://www.courtvision.dev",
    "http://localhost:3000",
    PREVIEW,        # branch alias
    DEPLOYMENT,     # one deployment
])
def test_the_origins_we_serve(client, origin):
    res = _preflight(client, origin)
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == origin
    assert res.headers["access-control-allow-credentials"] == "true"


@pytest.mark.api
@pytest.mark.parametrize("origin", [
    # Another team's Vercel project: the whole reason the regex is not `*.vercel.app`.
    "https://courtvision-git-main-someone-elses-team.vercel.app",
    "https://evil.vercel.app",
    # The anchors: a lookalike that merely *starts* or *ends* with the real thing.
    "https://courtvision-git-main-jameslk3s-projects.vercel.app.attacker.test",
    "https://attacker.test/https://courtvision-git-main-jameslk3s-projects.vercel.app",
    # A different Vercel project under the same team.
    "https://jlkendrick-git-main-jameslk3s-projects.vercel.app",
    # Right shape, wrong scheme.
    "http://courtvision-git-main-jameslk3s-projects.vercel.app",
    "https://courtvision.dev.attacker.test",
])
def test_the_origins_we_refuse(client, origin):
    res = _preflight(client, origin)
    assert "access-control-allow-origin" not in res.headers, origin
