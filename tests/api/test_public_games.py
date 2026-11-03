"""
API tests for GET /v1/games/{game_date}

Covers:
- Successful response for a valid date
- Invalid date format returns 422
- Service result is passed through to response
"""

import pytest
from datetime import date  # used for captured["game_date"] assertion

from schemas.common import ApiStatus
from schemas.games import GamesOnDateResp, GamesOnDateData


FAKE_GAMES_RESP = GamesOnDateResp(
    status=ApiStatus.SUCCESS,
    message="Games fetched",
    data=GamesOnDateData(
        date="2026-03-04",
        games=[],
        count=0,
    ),
)


@pytest.mark.api
def test_games_on_date_returns_200(client, monkeypatch):
    from services import games_service

    async def fake_get_games(game_date):
        return FAKE_GAMES_RESP

    monkeypatch.setattr(games_service.GamesService, "get_games_on_date", staticmethod(fake_get_games))

    res = client.get("/v1/games/2026-03-04")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["date"] == "2026-03-04"
    assert body["data"]["count"] == 0


@pytest.mark.api
def test_games_on_date_passes_date_to_service(client, monkeypatch):
    from services import games_service

    captured = {}

    async def fake_get_games(game_date):
        captured["game_date"] = game_date
        return FAKE_GAMES_RESP

    monkeypatch.setattr(games_service.GamesService, "get_games_on_date", staticmethod(fake_get_games))

    client.get("/v1/games/2026-03-04")
    assert captured["game_date"] == date(2026, 3, 4)


@pytest.mark.api
@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-13-01", "20260304", "march-4"])
def test_games_invalid_date_returns_422(client, bad_date):
    """FastAPI path validation rejects non-date strings."""
    res = client.get(f"/v1/games/{bad_date}")
    assert res.status_code == 422


# ---- GET /v1/games/{game_date}/{game_id} --------------------------------------


@pytest.mark.api
def test_one_game_passes_the_night_and_the_id_to_the_service(client, monkeypatch):
    from schemas.games import GameDetailResp
    from services import game_detail_service

    captured = {}

    async def fake_get_game(game_date, game_id):
        captured.update(game_date=game_date, game_id=game_id)
        return GameDetailResp(status=ApiStatus.NOT_FOUND, message="No game", data=None)

    monkeypatch.setattr(game_detail_service.GameDetailService, "get_game", staticmethod(fake_get_game))

    res = client.get("/v1/games/2026-10-20/0022600001")
    assert res.status_code == 404
    assert captured == {"game_date": date(2026, 10, 20), "game_id": "0022600001"}


@pytest.mark.api
@pytest.mark.parametrize("bad_id", ["22600001", "002260000x", "00226000011", "BOS@DET"])
def test_one_game_rejects_anything_but_a_ten_digit_game_id(client, bad_id):
    res = client.get(f"/v1/games/2026-10-20/{bad_id}")
    assert res.status_code == 422
