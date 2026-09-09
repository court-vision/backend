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


@pytest.mark.api
class TestBulkMarketContracts:
    @pytest.mark.parametrize("path,ref", [
        ("/v1/players/projections", "PlayerProjectionsResp"),
        ("/v1/rankings/espn/movement", "ESPNMarketMovementResp"),
    ])
    def test_new_reads_have_concrete_response_schemas(self, paths, path, ref):
        schema = _response_schema(paths, path)
        assert schema is not None
        assert schema.get("$ref", "").endswith(f"/{ref}"), schema

    def test_movement_dates_are_required_query_parameters(self, paths):
        parameters = {
            parameter["name"]: parameter
            for parameter in paths["/v1/rankings/espn/movement"]["get"]["parameters"]
        }
        assert parameters["from_as_of"]["required"] is True
        assert parameters["to_as_of"]["required"] is True


@pytest.mark.api
class TestSchemaNamesDoNotCollide:
    """Two models with one class name make the export non-deterministic.

    FastAPI keeps one under the bare class name and qualifies the other by
    module (`schemas__player__GameLog`), and which one wins follows hash
    ordering — so the same code exported two different schemas from one process
    to the next. That made the frontend's checked-in snapshot differ from
    production with nothing having changed, and meant production's own
    `/openapi.json` could flip on a restart.
    """

    def test_no_schema_name_is_module_qualified(self, schemas):
        qualified = sorted(k for k in schemas if "__" in k)
        assert qualified == [], (
            "These schema names are module-qualified, which FastAPI only does when two "
            f"models share a class name: {qualified}. Rename one of each colliding pair — "
            "while the collision stands, the export is not reproducible."
        )

    def test_one_game_log_model_serves_both_endpoints(self, schemas):
        # The collision that prompted the rule was two `GameLog` models, one per
        # endpoint. There is one now, so the endpoints cannot describe a game
        # differently and there is no name left to resolve by luck.
        assert "PlayerStatsGameLog" not in schemas
        assert {"game_id", "opponent", "home"} <= set(schemas["GameLog"]["properties"])
        for holder, field in (("PlayerStats", "game_logs"), ("PlayerGamesData", "games")):
            ref = schemas[holder]["properties"][field]["items"]["$ref"]
            assert ref.endswith("/GameLog"), f"{holder}.{field} -> {ref}"


@pytest.mark.api
class TestRosterWriteContracts:
    """The two team-scoped write-side routes the streamers page calls (PR-B)."""

    @pytest.mark.parametrize("path,ref", [
        ("/v1/internal/teams/{team_id}/streamers/find", "StreamerResp"),
        ("/v1/internal/teams/{team_id}/roster/transactions", "RosterTransactionResp"),
    ])
    def test_declare_concrete_response_schemas(self, paths, path, ref):
        schema = _response_schema(paths, path, "post")
        assert schema is not None, f"POST {path} has no 200 schema"
        assert schema.get("$ref", "").endswith(f"/{ref}"), schema

    def test_team_scoped_streamers_body_cannot_carry_a_league(self, paths, schemas):
        body = paths["/v1/internal/teams/{team_id}/streamers/find"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("/StreamerFindReq"), body
        assert "league_info" not in schemas["StreamerFindReq"]["properties"]
        assert "league_info" in schemas["StreamerReq"]["properties"]      # the legacy route keeps it

    def test_transaction_request_and_data_shapes(self, schemas):
        req = schemas["RosterTransactionReq"]["properties"]
        assert set(req) == {"add_player_id", "drop_player_id", "expected_scoring_period_id", "roster_version"}
        assert set(schemas["RosterTransactionReq"]["required"]) == {"expected_scoring_period_id", "roster_version"}
        data = schemas["RosterTransactionData"]["properties"]
        assert set(data) == {"lineup", "added", "dropped", "verified", "audit_id", "scoring_period_id"}
        assert data["lineup"]["$ref"].endswith("/LineupState")
        assert set(schemas["StreamerPlayerResp"]["properties"]) >= {"acquisition_status", "waivers_until"}
        assert set(schemas["StreamerData"]["properties"]) >= {"start_date", "end_date", "upcoming"}
