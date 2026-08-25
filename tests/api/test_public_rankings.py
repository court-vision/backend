"""
API tests for GET /v1/rankings/

Covers:
- Successful response shape (points and categories)
- window / format / categories / min_games query param passthrough
- FastAPI validation rejection for bad window, format, category keys, min_games
- meta echoed in the response
"""

from datetime import date

import pytest

from schemas.common import ApiStatus, CategoryDefResp
from schemas.rankings import RankingsMeta, RankingsPlayer, RankingsResp


FAKE_RANKINGS_DATA = [
    RankingsPlayer(id=1, rank=1, player_name="Nikola Jokic", team="DEN", total_fpts=2500.0, avg_fpts=62.5),
    RankingsPlayer(id=2, rank=2, player_name="Shai Gilgeous-Alexander", team="OKC", total_fpts=2300.0, avg_fpts=57.5),
]

FAKE_RANKINGS_RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="Rankings fetched",
    data=FAKE_RANKINGS_DATA,
)

FAKE_CATEGORY_RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="L14 category rankings fetched",
    data=[
        RankingsPlayer(
            id=1, rank=1, player_name="Nikola Jokic", team="DEN", total_fpts=375.0, avg_fpts=62.5,
            gp=6, categories={"pts": 28.5, "fg_pct": 0.5812, "tov": 3.2}, category_z={"pts": 2.1, "fg_pct": 3.4, "tov": -0.8},
            score=4.7,
        ),
    ],
    meta=RankingsMeta(
        format="categories", window=14, as_of=date(2026, 3, 4),
        categories=[
            CategoryDefResp(key="pts", label="PTS", higher_is_better=True, is_rate=False),
            CategoryDefResp(key="fg_pct", label="FG%", higher_is_better=True, is_rate=True),
            CategoryDefResp(key="tov", label="TO", higher_is_better=False, is_rate=False),
        ],
        pool_size=180, min_games=4,
    ),
)


def _patch_rankings(monkeypatch, response=FAKE_RANKINGS_RESP):
    """Stub RankingsService.get_rankings, returning a dict of the kwargs it was called with."""
    from services import rankings_service

    captured = {}

    async def fake_get_rankings(window=None, format="points", categories=None, min_games=None):
        captured.update(window=window, format=format, categories=categories, min_games=min_games)
        return response

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))
    return captured


@pytest.mark.api
def test_rankings_returns_200(client, monkeypatch):
    _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert len(body["data"]) == 2
    assert body["data"][0]["player_name"] == "Nikola Jokic"
    assert body["data"][0]["rank"] == 1
    assert body["meta"] is None


@pytest.mark.api
@pytest.mark.parametrize("window", [7, 14, 30])
def test_rankings_window_param_is_passed_through(client, monkeypatch, window):
    captured = _patch_rankings(monkeypatch)

    res = client.get(f"/v1/rankings/?window={window}")
    assert res.status_code == 200
    assert captured["window"] == window


@pytest.mark.api
def test_rankings_no_window_passes_none_and_defaults_to_points(client, monkeypatch):
    captured = _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/")
    assert res.status_code == 200
    assert captured == {"window": None, "format": "points", "categories": None, "min_games": None}


@pytest.mark.api
@pytest.mark.parametrize("window", [3, 6, 12, 31, 100, "abc"])
def test_rankings_invalid_window_returns_422(client, monkeypatch, window):
    """window must be exactly 7, 14, or 30 — anything else is rejected, never silently season."""
    captured = _patch_rankings(monkeypatch)

    res = client.get(f"/v1/rankings/?window={window}")
    assert res.status_code == 422
    assert captured == {}


@pytest.mark.api
def test_rankings_category_params_are_passed_through(client, monkeypatch):
    captured = _patch_rankings(monkeypatch, FAKE_CATEGORY_RESP)

    res = client.get("/v1/rankings/?format=categories&window=14&categories=pts,fg_pct,tov&min_games=4")
    assert res.status_code == 200
    assert captured == {"window": 14, "format": "categories", "categories": ["pts", "fg_pct", "tov"], "min_games": 4}


@pytest.mark.api
def test_rankings_categories_csv_is_normalized(client, monkeypatch):
    captured = _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/?format=categories&categories=%20PTS%20,reb,,pts")
    assert res.status_code == 200
    assert captured["categories"] == ["pts", "reb"]


@pytest.mark.api
def test_rankings_empty_categories_means_default(client, monkeypatch):
    captured = _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/?format=categories&categories=")
    assert res.status_code == 200
    assert captured["categories"] is None


@pytest.mark.api
def test_rankings_bogus_format_returns_422(client, monkeypatch):
    captured = _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/?format=bogus")
    assert res.status_code == 422
    assert captured == {}


@pytest.mark.api
def test_rankings_unknown_category_returns_422_listing_allowed_keys(client, monkeypatch):
    captured = _patch_rankings(monkeypatch)

    res = client.get("/v1/rankings/?format=categories&categories=pts,dd")
    assert res.status_code == 422
    assert captured == {}
    detail = res.json()["detail"]
    assert "dd" in detail and "fg_pct" in detail and "tov" in detail


@pytest.mark.api
@pytest.mark.parametrize("min_games", [0, 83, "x"])
def test_rankings_invalid_min_games_returns_422(client, monkeypatch, min_games):
    captured = _patch_rankings(monkeypatch)

    res = client.get(f"/v1/rankings/?format=categories&min_games={min_games}")
    assert res.status_code == 422
    assert captured == {}


@pytest.mark.api
def test_rankings_meta_and_category_fields_are_echoed(client, monkeypatch):
    _patch_rankings(monkeypatch, FAKE_CATEGORY_RESP)

    res = client.get("/v1/rankings/?format=categories&window=14")
    assert res.status_code == 200
    body = res.json()

    meta = body["meta"]
    assert meta["format"] == "categories"
    assert meta["window"] == 14
    assert meta["as_of"] == "2026-03-04"
    assert meta["pool_size"] == 180
    assert meta["min_games"] == 4
    assert [c["key"] for c in meta["categories"]] == ["pts", "fg_pct", "tov"]
    assert meta["categories"][2]["higher_is_better"] is False

    row = body["data"][0]
    assert row["gp"] == 6
    assert row["categories"] == {"pts": 28.5, "fg_pct": 0.5812, "tov": 3.2}
    assert row["category_z"]["tov"] == -0.8
    assert row["score"] == 4.7


@pytest.mark.api
def test_points_rows_keep_legacy_shape_with_category_fields_null(client, monkeypatch):
    _patch_rankings(monkeypatch)

    row = client.get("/v1/rankings/").json()["data"][0]
    assert row["total_fpts"] == 2500.0 and row["avg_fpts"] == 62.5 and row["rank_change"] == 0
    assert row["gp"] is None and row["categories"] is None and row["category_z"] is None and row["score"] is None
