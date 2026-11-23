"""
API tests for GET /v1/rankings/live and GET /v1/rankings/live/players/{player_id}.

The caching contract (ETag, 304, errors never cached) is covered with every
other cached public read in test_public_read_cache.py; these are the routes'
own: the envelope, what reaches the service, validation and the OpenAPI shapes.
"""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from schemas.common import ApiStatus
from schemas.live_rankings import LivePlayerRankData, LivePlayerRankResp, LiveRankingsData, LiveRankingsResp
from services.live_rankings_service import LiveRankingsService

pytestmark = pytest.mark.api

BOARD = LiveRankingsResp.model_validate({
    "status": "success", "message": "Court Vision live rankings",
    "data": {
        "season": "2026-27", "as_of": date(2026, 11, 21), "model": "live-1", "espn_as_of": date(2026, 11, 21),
        "compared": {"day": date(2026, 11, 20), "week": date(2026, 11, 14)},
        "players": [{
            "player_id": 203999, "name": "Nikola Jokić", "team": "DEN", "position": "C", "injury_status": None,
            "season_gp": 14, "games": 61.25,
            "points": {"rank": 1, "value": 62.5, "move_1d": 0, "move_7d": 2, "espn_rank": 1},
            "categories": {"rank": 2, "value": 1.38, "move_1d": -1, "move_7d": None, "espn_rank": 3},
        }],
    },
})


def test_the_board_comes_back_whole(client, monkeypatch):
    service = AsyncMock(return_value=BOARD)
    monkeypatch.setattr(LiveRankingsService, "get_board", service)

    res = client.get("/v1/rankings/live")

    assert res.status_code == 200
    body = res.json()
    assert (body["status"], body["data"]["as_of"], body["data"]["model"]) == ("success", "2026-11-21", "live-1")
    assert body["data"]["compared"] == {"day": "2026-11-20", "week": "2026-11-14"}
    assert body["data"]["players"][0]["points"] == {"rank": 1, "value": 62.5, "move_1d": 0, "move_7d": 2, "espn_rank": 1}
    assert body["data"]["players"][0]["categories"]["move_7d"] is None       # null, never zero
    service.assert_awaited_once_with()


def test_no_caller_can_name_a_source_or_a_day(client, monkeypatch):
    """The table also holds boards graded in shadow. The route takes no
    parameter at all, so there is nothing a caller could point at one with."""
    service = AsyncMock(return_value=BOARD)
    monkeypatch.setattr(LiveRankingsService, "get_board", service)

    res = client.get("/v1/rankings/live?source=cv_shadow&as_of=2026-11-01&board=shadow")

    assert res.status_code == 200
    service.assert_awaited_once_with()


def test_a_season_with_no_board_yet_is_a_success_with_no_players(client, monkeypatch):
    empty = LiveRankingsResp(status=ApiStatus.SUCCESS, message="No live rankings stored for 2026-27 yet",
                             data=LiveRankingsData(season="2026-27"))
    monkeypatch.setattr(LiveRankingsService, "get_board", AsyncMock(return_value=empty))

    body = client.get("/v1/rankings/live").json()

    assert body["status"] == "success" and body["message"] == "No live rankings stored for 2026-27 yet"
    assert body["data"] == {"season": "2026-27", "as_of": None, "model": None, "espn_as_of": None,
                            "compared": {"day": None, "week": None}, "players": []}


def test_a_players_sheet_is_asked_for_by_his_nba_id(client, monkeypatch):
    sheet = LivePlayerRankResp(
        status=ApiStatus.SUCCESS, message="Court Vision live rankings for one player",
        data=LivePlayerRankData(player_id=203999, name="Nikola Jokić", season="2026-27", as_of=date(2026, 11, 21)),
    )
    service = AsyncMock(return_value=sheet)
    monkeypatch.setattr(LiveRankingsService, "get_player", service)

    res = client.get("/v1/rankings/live/players/203999")

    assert res.status_code == 200
    data = res.json()["data"]
    assert (data["player_id"], data["points"], data["history"]) == (203999, None, [])
    assert data["espn"] == {"as_of": None, "points": None, "categories": None}
    service.assert_awaited_once_with(203999)


@pytest.mark.parametrize("player_id", ["0", "-4", "jokic"])
def test_a_player_id_that_cannot_be_one_is_a_422(client, monkeypatch, player_id):
    service = AsyncMock()
    monkeypatch.setattr(LiveRankingsService, "get_player", service)

    assert client.get(f"/v1/rankings/live/players/{player_id}").status_code == 422
    service.assert_not_awaited()


def test_both_reads_have_concrete_response_schemas(app):
    paths = app.openapi()["paths"]

    for path, ref in (("/v1/rankings/live", "LiveRankingsResp"),
                      ("/v1/rankings/live/players/{player_id}", "LivePlayerRankResp")):
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith(f"/{ref}"), schema
    # The board takes no parameters; the sheet takes the player and nothing else
    assert paths["/v1/rankings/live"]["get"].get("parameters", []) == []
    assert [p["name"] for p in paths["/v1/rankings/live/players/{player_id}"]["get"]["parameters"]] == ["player_id"]
