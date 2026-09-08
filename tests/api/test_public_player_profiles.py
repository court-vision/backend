"""HTTP contracts for dimension-backed player search and profiles."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from schemas.common import ApiStatus
from schemas.player_profiles import (
    PlayerProfileData,
    PlayerProfileResp,
    PlayerSearchData,
    PlayerSearchItem,
    PlayerSearchResp,
)
from services.player_profile_service import PlayerProfileService

pytestmark = pytest.mark.api
NOW = datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)


def test_search_route_uses_the_dimension_service(client, monkeypatch):
    response = PlayerSearchResp(
        status=ApiStatus.SUCCESS,
        message="Found 1 players",
        data=PlayerSearchData(
            query="Flagg",
            players=[
                PlayerSearchItem(
                    id=1642845,
                    espn_id=5105552,
                    name="Cooper Flagg",
                    position="F",
                    team="DAL",
                    player_updated_at=NOW,
                    profile_updated_at=NOW,
                )
            ],
            total=1,
            limit=5,
            offset=2,
        ),
    )
    search = AsyncMock(return_value=response)
    monkeypatch.setattr(PlayerProfileService, "search_players", search)

    result = client.get("/v1/players/search?q=Flagg&limit=5&offset=2")

    assert result.status_code == 200
    assert result.json()["data"]["players"][0]["espn_id"] == 5105552
    assert result.json()["data"]["players"][0]["profile_updated_at"].endswith("Z")
    search.assert_awaited_once_with(q="Flagg", limit=5, offset=2)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/players/search",
        "/v1/players/search?q=",
        "/v1/players/search?q=%20%20%20",
        "/v1/players/search?q=James&limit=0",
        "/v1/players/search?q=James&limit=51",
        "/v1/players/search?q=James&offset=-1",
    ],
)
def test_search_parameters_are_validated_before_service_calls(client, monkeypatch, path):
    search = AsyncMock()
    monkeypatch.setattr(PlayerProfileService, "search_players", search)

    result = client.get(path)

    assert result.status_code == 422
    search.assert_not_awaited()


def test_profile_route_returns_identity_without_requiring_a_profile(client, monkeypatch):
    response = PlayerProfileResp(
        status=ApiStatus.SUCCESS,
        message="Player profile",
        data=PlayerProfileData(
            id=1642845,
            espn_id=5105552,
            name="Cooper Flagg",
            position="F",
            created_at=NOW,
            updated_at=NOW,
            profile=None,
        ),
    )
    get_profile = AsyncMock(return_value=response)
    monkeypatch.setattr(PlayerProfileService, "get_profile", get_profile)

    result = client.get("/v1/players/1642845/profile")

    assert result.status_code == 200
    assert result.json()["data"]["id"] == 1642845
    assert result.json()["data"]["profile"] is None
    get_profile.assert_awaited_once_with(player_id=1642845)


def test_profile_player_id_must_be_positive(client, monkeypatch):
    get_profile = AsyncMock()
    monkeypatch.setattr(PlayerProfileService, "get_profile", get_profile)

    assert client.get("/v1/players/0/profile").status_code == 422
    get_profile.assert_not_awaited()


def test_new_routes_have_concrete_openapi_response_models(app):
    paths = app.openapi()["paths"]
    expected = {
        "/v1/players/search": "PlayerSearchResp",
        "/v1/players/{player_id}/profile": "PlayerProfileResp",
    }
    for path, model in expected.items():
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith(f"/{model}")
