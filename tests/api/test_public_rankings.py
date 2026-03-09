"""
API tests for GET /v1/rankings/

Covers:
- Successful response shape
- window query param passthrough (7, 14, 30)
- FastAPI validation rejection for out-of-range window
"""

import pytest

from schemas.common import ApiStatus
from schemas.rankings import RankingsResp, RankingsPlayer


FAKE_RANKINGS_DATA = [
    RankingsPlayer(id=1, rank=1, player_name="Nikola Jokic", team="DEN", total_fpts=2500.0, avg_fpts=62.5),
    RankingsPlayer(id=2, rank=2, player_name="Shai Gilgeous-Alexander", team="OKC", total_fpts=2300.0, avg_fpts=57.5),
]

FAKE_RANKINGS_RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="Rankings fetched",
    data=FAKE_RANKINGS_DATA,
)


@pytest.mark.api
def test_rankings_returns_200(client, monkeypatch):
    from services import rankings_service

    async def fake_get_rankings(window=None):
        return FAKE_RANKINGS_RESP

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))

    res = client.get("/v1/rankings/")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert len(body["data"]) == 2
    assert body["data"][0]["player_name"] == "Nikola Jokic"
    assert body["data"][0]["rank"] == 1


@pytest.mark.api
@pytest.mark.parametrize("window", [7, 14, 30])
def test_rankings_window_param_is_passed_through(client, monkeypatch, window):
    from services import rankings_service

    captured = {}

    async def fake_get_rankings(window=None):
        captured["window"] = window
        return FAKE_RANKINGS_RESP

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))

    res = client.get(f"/v1/rankings/?window={window}")
    assert res.status_code == 200
    assert captured["window"] == window


@pytest.mark.api
def test_rankings_no_window_passes_none(client, monkeypatch):
    from services import rankings_service

    captured = {}

    async def fake_get_rankings(window=None):
        captured["window"] = window
        return FAKE_RANKINGS_RESP

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))

    res = client.get("/v1/rankings/")
    assert res.status_code == 200
    assert captured.get("window") is None


@pytest.mark.api
@pytest.mark.parametrize("window", [3, 6, 31, 100])
def test_rankings_invalid_window_returns_422(client, window):
    """FastAPI validation: window must be between 7 and 30."""
    res = client.get(f"/v1/rankings/?window={window}")
    assert res.status_code == 422
