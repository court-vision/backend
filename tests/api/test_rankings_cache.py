"""
GET /v1/rankings/ response caching.

The contract that matters: a cached body is byte-for-byte what FastAPI's
`response_model` would have sent, so caching cannot change the API.
"""

from datetime import date

import pytest

from schemas.common import ApiStatus, CategoryDefResp
from schemas.rankings import RankingsMeta, RankingsPlayer, RankingsResp

POINTS_RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="Rankings fetched successfully",
    data=[
        RankingsPlayer(id=1, rank=1, player_name="Nikola Jokić", team="DEN",
                       total_fpts=2500.0, avg_fpts=62.5, rank_change=2, gp=40),
        RankingsPlayer(id=2, rank=2, player_name="Shai Gilgeous-Alexander", team="OKC",
                       total_fpts=2300.0, avg_fpts=57.5),
    ],
    meta=RankingsMeta(format="points", as_of=date(2026, 3, 4), pool_size=2,
                      season="2025-26", season_day=135, max_gp=40),
)

CATEGORY_RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="L14 category rankings fetched successfully (as of 2026-03-04)",
    data=[
        RankingsPlayer(
            id=1, rank=1, player_name="Nikola Jokić", team="DEN", total_fpts=375.0, avg_fpts=62.5,
            gp=6, categories={"pts": 28.5, "fg_pct": 0.5812, "ft_pct": None},
            category_z={"pts": 2.1, "fg_pct": 3.4, "ft_pct": 0.0}, score=5.5,
        ),
    ],
    meta=RankingsMeta(
        format="categories", window=14, as_of=date(2026, 3, 4),
        categories=[CategoryDefResp(key="pts", label="PTS", higher_is_better=True, is_rate=False)],
        pool_size=180, min_games=1, season="2025-26", season_day=135, max_gp=12,
    ),
)

BAD_REQUEST_RESP = RankingsResp(
    status=ApiStatus.BAD_REQUEST, message="Invalid format 'roto'; use one of ['points', 'categories']", data=[],
)


@pytest.fixture
def service_calls(monkeypatch):
    """Stub the service and count calls; `response` is settable per test."""
    from services import rankings_service

    state = {"response": POINTS_RESP, "calls": []}

    async def fake_get_rankings(window=None, format="points", categories=None, min_games=None):
        state["calls"].append({"window": window, "format": format,
                               "categories": categories, "min_games": min_games})
        return state["response"]

    monkeypatch.setattr(rankings_service.RankingsService, "get_rankings", staticmethod(fake_get_rankings))
    return state


@pytest.mark.api
def test_cached_body_is_byte_identical_to_the_response_model_rendering(client, service_calls, monkeypatch):
    """The cache serialises the envelope itself; it must match FastAPI exactly,
    non-ASCII names, nulls and all."""
    from api.v1.public import rankings

    service_calls["response"] = CATEGORY_RESP
    url = "/v1/rankings/?format=categories&window=14"

    monkeypatch.setattr(rankings.RESPONSE_CACHE, "ttl_seconds", 0.0)   # kill switch: FastAPI renders
    uncached = client.get(url)
    monkeypatch.setattr(rankings.RESPONSE_CACHE, "ttl_seconds", 300.0)
    miss = client.get(url)
    hit = client.get(url)

    assert uncached.status_code == miss.status_code == hit.status_code == 200
    assert uncached.content == miss.content == hit.content
    assert uncached.headers["content-type"] == miss.headers["content-type"]
    assert hit.json()["data"][0]["categories"] == {"pts": 28.5, "fg_pct": 0.5812, "ft_pct": None}


@pytest.mark.api
def test_a_hit_does_not_reach_the_service(client, service_calls):
    first = client.get("/v1/rankings/")
    second = client.get("/v1/rankings/")

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert len(service_calls["calls"]) == 1
    assert first.json() == second.json()


@pytest.mark.api
def test_each_query_variant_is_cached_separately(client, service_calls):
    for url in ("/v1/rankings/",
                "/v1/rankings/?window=7",
                "/v1/rankings/?format=categories",
                "/v1/rankings/?format=categories&categories=pts,reb",
                "/v1/rankings/?format=categories&min_games=10"):
        assert client.get(url).headers["x-cache"] == "MISS"
        assert client.get(url).headers["x-cache"] == "HIT"

    assert [c["window"] for c in service_calls["calls"]] == [None, 7, None, None, None]
    assert [c["categories"] for c in service_calls["calls"]] == [None, None, None, ["pts", "reb"], None]
    assert [c["min_games"] for c in service_calls["calls"]] == [None, None, None, None, 10]


@pytest.mark.api
def test_equivalent_category_spellings_share_one_entry(client, service_calls):
    """`categories` is normalised before it becomes part of the key."""
    service_calls["response"] = CATEGORY_RESP

    assert client.get("/v1/rankings/?format=categories&categories=pts,reb").headers["x-cache"] == "MISS"
    assert client.get("/v1/rankings/?format=categories&categories=%20PTS%20,reb,,pts").headers["x-cache"] == "HIT"
    assert len(service_calls["calls"]) == 1


@pytest.mark.api
def test_an_error_envelope_is_never_cached(client, service_calls):
    service_calls["response"] = BAD_REQUEST_RESP

    first = client.get("/v1/rankings/")
    second = client.get("/v1/rankings/")

    assert first.status_code == second.status_code == 400
    assert first.json()["error_code"] == "BAD_REQUEST"
    assert len(service_calls["calls"]) == 2      # re-asked every time
    assert "x-cache" not in first.headers


@pytest.mark.api
def test_etag_round_trip_answers_304_without_a_body(client, service_calls):
    first = client.get("/v1/rankings/")
    etag = first.headers["etag"]

    conditional = client.get("/v1/rankings/", headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == etag

    # A weak validator for the same tag, and `*`, match too.
    assert client.get("/v1/rankings/", headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    assert client.get("/v1/rankings/", headers={"If-None-Match": "*"}).status_code == 304
    assert client.get("/v1/rankings/", headers={"If-None-Match": '"stale"'}).status_code == 200
    assert len(service_calls["calls"]) == 1


@pytest.mark.api
def test_cache_control_never_outlives_the_servers_own_entry(client, service_calls):
    from api.v1.public import rankings

    ttl = rankings.RESPONSE_CACHE.ttl_seconds
    res = client.get("/v1/rankings/")
    max_age = int(res.headers["cache-control"].removeprefix("public, max-age="))

    assert 0 < max_age <= ttl
    assert res.headers["cache-control"].startswith("public, ")


@pytest.mark.api
def test_a_cleared_cache_falls_back_to_the_service(client, service_calls):
    from api.v1.public import rankings

    client.get("/v1/rankings/")
    rankings.RESPONSE_CACHE.clear()
    assert client.get("/v1/rankings/").headers["x-cache"] == "MISS"
    assert len(service_calls["calls"]) == 2

@pytest.mark.api
def test_points_requests_are_not_fragmented_by_category_only_params(client, service_calls):
    """`categories` and `min_games` change nothing about a points response."""
    assert client.get("/v1/rankings/").headers["x-cache"] == "MISS"
    assert client.get("/v1/rankings/?min_games=40").headers["x-cache"] == "HIT"
    assert client.get("/v1/rankings/?categories=pts,reb").headers["x-cache"] == "HIT"
    assert len(service_calls["calls"]) == 1
