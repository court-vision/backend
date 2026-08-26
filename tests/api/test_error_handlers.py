"""
Every exception class renders as the standard envelope with the right HTTP
status, `error_code`, `data.correlation_id`, and the `X-Correlation-ID` /
`X-Error-Code` headers. A 500 never leaks exception text.
"""

import uuid

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from peewee import OperationalError

from core.errors import (
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    ServiceUnavailableError,
)
from tests.api.conftest import make_test_app

# kind -> (raise, expected http status, expected envelope status, expected error_code)
CASES = {
    "bad_request": (lambda: BadRequestError("LEAGUE_NOT_FOUND", "League not found"), 400, "bad_request", "LEAGUE_NOT_FOUND"),
    "auth": (lambda: AuthenticationError("TOKEN_EXPIRED", "Token has expired"), 401, "authentication_error", "TOKEN_EXPIRED"),
    "auth_default": (lambda: AuthenticationError(), 401, "authentication_error", "AUTH_REQUIRED"),
    "forbidden": (lambda: AuthorizationError(), 403, "authorization_error", "FORBIDDEN"),
    "provider_auth": (lambda: ProviderAuthError("yahoo"), 403, "authorization_error", "PROVIDER_AUTH_EXPIRED"),
    "not_found": (lambda: NotFoundError("TEAM_NOT_FOUND", "Team not found"), 404, "not_found", "TEAM_NOT_FOUND"),
    "conflict": (lambda: ConflictError(), 409, "conflict", "CONFLICT"),
    "provider": (lambda: ProviderError("espn"), 502, "server_error", "PROVIDER_UNAVAILABLE"),
    "provider_bad": (lambda: ProviderError("espn", error_code="PROVIDER_BAD_RESPONSE"), 502, "server_error", "PROVIDER_BAD_RESPONSE"),
    "provider_timeout": (lambda: ProviderTimeout("espn"), 504, "server_error", "PROVIDER_TIMEOUT"),
    "unavailable": (lambda: ServiceUnavailableError("AUTH_UNAVAILABLE"), 503, "server_error", "AUTH_UNAVAILABLE"),
}


def _app_with_raisers():
    app = make_test_app()
    router = APIRouter(prefix="/boom")

    @router.get("/app/{kind}")
    async def raise_app(kind: str):
        raise CASES[kind][0]()

    @router.get("/http")
    async def raise_http():
        raise HTTPException(status_code=418, detail="teapot", headers={"X-Teapot": "yes"})

    @router.get("/db")
    async def raise_db():
        raise OperationalError("server closed the connection unexpectedly")

    @router.get("/crash")
    async def crash():
        raise RuntimeError("secret-internal-detail")

    @router.get("/validate")
    async def validate(n: int):
        return {"n": n}

    app.include_router(router)
    return app


@pytest.fixture
def raiser():
    return TestClient(_app_with_raisers(), raise_server_exceptions=False)


@pytest.mark.api
@pytest.mark.parametrize("kind", list(CASES))
def test_app_errors_render_the_envelope(raiser, kind):
    _, status, api_status, code = CASES[kind]
    res = raiser.get(f"/boom/app/{kind}", headers={"X-Correlation-ID": f"t-{kind}"})

    assert res.status_code == status
    body = res.json()
    assert set(body) >= {"status", "message", "data", "error_code"}
    assert body["status"] == api_status
    assert body["error_code"] == code
    assert body["message"]
    assert body["data"]["correlation_id"] == f"t-{kind}"
    assert res.headers["X-Correlation-ID"] == f"t-{kind}"
    assert res.headers["X-Error-Code"] == code


@pytest.mark.api
def test_provider_auth_error_names_the_provider(raiser):
    body = raiser.get("/boom/app/provider_auth").json()
    assert body["data"]["provider"] == "yahoo"
    assert "reconnect" in body["message"].lower()


@pytest.mark.api
def test_correlation_id_is_generated_when_absent(raiser):
    res = raiser.get("/boom/app/not_found")
    cid = res.headers["X-Correlation-ID"]
    assert uuid.UUID(cid)  # a real UUID, not empty
    assert res.json()["data"]["correlation_id"] == cid


@pytest.mark.api
def test_http_exception_keeps_status_and_headers(raiser):
    res = raiser.get("/boom/http")
    assert res.status_code == 418
    body = res.json()
    assert body["message"] == "teapot"
    assert body["error_code"] == "HTTP_418"
    assert body["status"] == "error"
    assert res.headers["X-Teapot"] == "yes"
    assert res.headers["X-Error-Code"] == "HTTP_418"


@pytest.mark.api
def test_unknown_route_is_a_404_envelope(raiser):
    res = raiser.get("/nope")
    assert res.status_code == 404
    body = res.json()
    assert body["status"] == "not_found"
    assert body["error_code"] == "HTTP_404"
    assert "detail" not in body


@pytest.mark.api
def test_database_error_is_503_database_unavailable(raiser):
    res = raiser.get("/boom/db")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "server_error"
    assert body["error_code"] == "DATABASE_UNAVAILABLE"
    assert "server closed" not in res.text
    assert res.headers["X-Error-Code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.api
def test_unhandled_exception_is_500_without_exception_text(raiser):
    res = raiser.get("/boom/crash", headers={"X-Correlation-ID": "t-500"})
    assert res.status_code == 500
    body = res.json()
    assert body["status"] == "server_error"
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "secret-internal-detail" not in res.text
    assert "RuntimeError" not in res.text
    assert body["data"]["correlation_id"] == "t-500"
    # ServerErrorMiddleware renders the handler's response, so the headers come from the handler
    assert res.headers["X-Correlation-ID"] == "t-500"
    assert res.headers["X-Error-Code"] == "INTERNAL_ERROR"


@pytest.mark.api
def test_validation_error_is_422_with_errors(raiser):
    res = raiser.get("/boom/validate?n=abc")
    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "validation_error"
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["message"] == "Request validation failed"
    errors = body["data"]["errors"]
    assert isinstance(errors, list) and errors[0]["loc"] == ["query", "n"]
    assert body["data"]["correlation_id"]


@pytest.mark.api
def test_success_responses_carry_the_correlation_header(raiser):
    res = raiser.get("/boom/validate?n=3", headers={"X-Correlation-ID": "t-ok"})
    assert res.status_code == 200
    assert res.headers["X-Correlation-ID"] == "t-ok"
    assert "X-Error-Code" not in res.headers
