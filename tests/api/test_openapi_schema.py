"""
The OpenAPI schema as a contract surface.

The frontend generates its API types from this schema (checked-in snapshot of
`scripts/export_openapi.py`), so these tests pin the two properties the
generated types depend on:

1. Endpoints the frontend consumes have concrete response schemas — a route
   without `response_model` generates `unknown` and silently exempts itself
   from the contract.
2. The team-write request model cannot carry raw Yahoo tokens. The credential
   boundary (7b) took tokens out of the browser; a request schema that still
   advertised them would type them right back into a generated client.
"""

import pytest

from main import app


@pytest.fixture(scope="module")
def schemas():
    return app.openapi()["components"]["schemas"]


@pytest.fixture(scope="module")
def paths():
    return app.openapi()["paths"]


def _response_schema(paths, path, method="get"):
    """The 200-response schema ref/inline for a route, or None."""
    op = paths[path][method]
    return (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )


@pytest.mark.api
class TestFormerBareDictEndpoints:
    """These six returned undocumented dicts until 2026-08; the frontend
    consumes four of them, so their hand-written TS types could never be
    replaced by codegen. Now they declare models the handlers must satisfy."""

    @pytest.mark.parametrize("path,method,ref", [
        ("/v1/live/players/today", "get", "LivePlayersResp"),
        ("/v1/live/schedule/today", "get", "LiveScheduleResp"),
        ("/v1/live/scoreboard", "get", "ScoreboardResp"),
        ("/v1/internal/api-keys/", "get", "ApiKeyListResp"),
        ("/v1/internal/api-keys/", "post", "CreateApiKeyResp"),
        ("/v1/internal/api-keys/{key_id}", "delete", "RevokeApiKeyResp"),
    ])
    def test_declares_a_concrete_response_schema(self, paths, path, method, ref):
        schema = _response_schema(paths, path, method)
        assert schema is not None, f"{method.upper()} {path} has no 200 schema"
        assert schema.get("$ref", "").endswith(f"/{ref}"), schema


@pytest.mark.api
class TestLeagueInfoWriteCannotCarryTokens:
    def test_no_token_fields_in_the_write_schema(self, schemas):
        props = schemas["LeagueInfoWrite"]["properties"]
        for forbidden in ("yahoo_access_token", "yahoo_refresh_token", "yahoo_token_expiry"):
            assert forbidden not in props, (
                f"{forbidden} is back in the team-write schema — a generated "
                "client would reintroduce raw tokens to the browser"
            )
        # The opaque handle is what the browser sends instead.
        assert "yahoo_connection_id" in props

    def test_team_write_routes_use_the_write_model(self, paths):
        for path, method in [("/v1/internal/teams/add", "post"), ("/v1/internal/teams/update", "put")]:
            body = paths[path][method]["requestBody"]["content"]["application/json"]["schema"]
            ref = body.get("$ref", "")
            req = ref.rsplit("/", 1)[-1]
            # The request model itself references LeagueInfoWrite, never LeagueInfo.
            assert req in ("TeamAddReq", "TeamUpdateReq"), ref

    def test_request_models_reference_write_not_internal(self, schemas):
        for req in ("TeamAddReq", "TeamUpdateReq"):
            league_ref = schemas[req]["properties"]["league_info"].get("$ref", "")
            assert league_ref.endswith("/LeagueInfoWrite"), league_ref


@pytest.mark.api
class TestEnvelopeFieldsAreRequired:
    """`json_schema_serialization_defaults_required` on BaseResponse: FastAPI
    always serializes every response field, so the schema says `required` and
    generated TS gets `data: X | null` instead of `data?: X | null`."""

    def test_envelope_fields_required_on_a_default_bearing_response(self, schemas):
        required = schemas["LivePlayersResp"].get("required", [])
        for field in ("status", "message", "data"):
            assert field in required
