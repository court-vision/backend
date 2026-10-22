"""
HTTP caching of the public reads Scout and Week make (core/public_read_cache.py).

The contracts that matter:
- a cached body is byte-for-byte what FastAPI's `response_model` sent before,
  so caching cannot change the API;
- every input that changes a body is in its key, so no entry answers for
  another request;
- errors, today's games (live overlay), injury status, ownership and the live
  endpoints are never cached.
"""

from datetime import date, datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from peewee import OperationalError

from core import public_read_cache as caches
from core.errors import NotFoundError
from core.settings import settings
from schemas.common import ApiStatus, BaseResponse
from schemas.games import GameDetailResp, GamesOnDateResp
from schemas.market import PlayerProjectionResp
from schemas.player import PlayerOwnershipResp, PlayerPercentilesResp, PlayerStatsResp, PlayerStatusResp
from schemas.player_profiles import PlayerProfileResp
from schemas.player_trends import PlayerTrendsResp
from schemas.teams import NBATeamLiveGameResp, NBATeamRosterResp, NBATeamStatsResp, TeamScheduleResp

pytestmark = pytest.mark.api

# Realistic envelopes: non-ASCII names, nulls, floats with and without a
# fraction, nested lists and dates, everything the serializers could disagree on.
PROFILE = PlayerProfileResp.model_validate({
    "status": "success", "message": "Player profile retrieved",
    "data": {
        "id": 203999, "espn_id": 3112335, "name": "Nikola Jokić", "position": "C",
        "created_at": datetime(2024, 10, 1, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 12, 9, 30, 15, 123456, tzinfo=timezone.utc),
        "profile": {
            "first_name": "Nikola", "last_name": "Jokić", "birthdate": date(1995, 2, 19), "height": "6-11",
            "height_inches": 83, "weight": 284, "position": "Center", "jersey_number": "15", "team": "DEN",
            "draft_year": 2014, "draft_round": 2, "draft_number": 41, "season_exp": 11, "country": "Serbia",
            "school": None, "from_year": 2015, "to_year": 2026,
            "updated_at": datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc),
        },
    },
})
PROJECTION = PlayerProjectionResp.model_validate({
    "status": "success", "message": "ESPN projection",
    "data": {
        "player_id": 203999, "espn_id": 3112335, "name": "Nikola Jokić", "season": "2025-26",
        "as_of_date": date(2026, 10, 1), "projected_gp": 72,
        "stats": {"min": 35.5, "pts": 28.0, "reb": 12.75, "ast": 9.1, "stl": None, "fg_pct": 0.5833333333333334},
    },
})
STATS = PlayerStatsResp.model_validate({
    "status": "success", "message": "Player stats fetched successfully",
    "data": {
        "id": 203999, "name": "Nikola Jokić", "team": "DEN", "games_played": 2, "window": "season", "window_games": 2,
        "avg_stats": {k: v for k, v in zip(
            ["avg_fpts", "avg_points", "avg_rebounds", "avg_assists", "avg_steals", "avg_blocks", "avg_turnovers",
             "avg_minutes", "avg_fg_pct", "avg_fg3_pct", "avg_ft_pct", "avg_ts_pct", "avg_efg_pct", "avg_three_rate",
             "avg_ft_rate", "avg_fgm", "avg_fga", "avg_fg3m", "avg_fg3a", "avg_ftm", "avg_fta"],
            [62.5, 29.0, 12.5, 10.0, 1.5, 0.5, 3.0, 36.1, 58.3, 41.7, 82.4, 66.2, 61.1, 20.0, 34.6,
             11.0, 18.0, 1.5, 3.5, 5.5, 6.5])},
        "advanced_stats": {"off_rating": 129.4, "def_rating": None, "usg_pct": 31.2, "pie": 22.0},
        "game_logs": [
            {"date": "2025-10-23", "game_id": "0022500004", "opponent": "GSW", "home": False, "fpts": 70,
             "pts": 31, "reb": 14, "ast": 11, "stl": 2, "blk": 1, "tov": 4, "min": 37, "fgm": 12, "fga": 19,
             "fg3m": 2, "fg3a": 4, "ftm": 5, "fta": 6},
            {"date": "2025-10-25", "game_id": None, "opponent": None, "home": None, "fpts": 55,
             "pts": 27, "reb": 11, "ast": 9, "stl": 1, "blk": 0, "tov": 2, "min": 35, "fgm": 10, "fga": 17,
             "fg3m": 1, "fg3a": 3, "ftm": 6, "fta": 7},
        ],
    },
})
TRENDS = PlayerTrendsResp.model_validate({
    "status": "success", "message": "Trends for Nikola Jokić",
    "data": {"player_id": 203999, "player_name": "Nikola Jokić", "team": "DEN", "current_rank": 1,
             "trends": {"last_7_days": {"avg_fpts": 61.3, "games": 3, "as_of_date": "2026-03-04"}},
             "ownership": None},
})
PERCENTILES = PlayerPercentilesResp.model_validate({
    "status": "success", "message": "Percentiles calculated successfully",
    "data": {k: 99 for k in ["avg_fpts", "avg_points", "avg_rebounds", "avg_assists", "avg_steals", "avg_blocks",
                             "avg_turnovers", "avg_minutes", "avg_fg_pct", "avg_fg3_pct", "avg_ft_pct", "avg_fg3m"]},
})
SCHEDULE = TeamScheduleResp.model_validate({
    "status": "success", "message": "Schedule for Denver Nuggets",
    "data": {"team": "DEN", "team_name": "Denver Nuggets", "remaining_games": 1, "total_games": 2, "schedule": [
        {"date": "2025-10-21", "opponent": "LAL", "home": True, "back_to_back": True, "status": "final",
         "team_score": 118, "opponent_score": 110, "opponent_def_rating": 112.4},
        {"date": "2025-10-23", "opponent": "BOS", "home": False, "back_to_back": False, "status": "scheduled",
         "team_score": None, "opponent_score": None, "opponent_def_rating": None},
    ]},
})
TEAM_STATS = NBATeamStatsResp.model_validate({
    "status": "success", "message": "Stats for Denver Nuggets",
    "data": {"team": "DEN", "team_name": "Denver Nuggets", "conference": "West", "division": "Northwest",
             "as_of_date": "2026-03-04", "season": "2025-26", "gp": 60, "w": 41, "l": 19, "w_pct": 0.683,
             "pts": 120.0, "def_rating": 111.5, "pie": None},
})
ROSTER = NBATeamRosterResp.model_validate({
    "status": "success", "message": "Roster for Denver Nuggets",
    "data": {"team": "DEN", "team_name": "Denver Nuggets", "as_of_date": "2026-03-04", "season": "2025-26",
             "players": [{"player_id": 203999, "name": "Nikola Jokić", "position": "C", "gp": 60, "pts": 29.6,
                          "reb": 12.7, "ast": 10.2, "stl": 1.4, "blk": 0.7, "tov": 3.1, "fpts": 62.0,
                          "fg_pct": 0.583, "fg3_pct": None, "ft_pct": 0.82, "injury_status": "Questionable"}]},
})
GAMES = GamesOnDateResp.model_validate({
    "status": "success", "message": "Found 1 games on 2026-03-04",
    "data": {"date": "2026-03-04", "count": 1, "games": [
        {"game_id": "0022500900", "game_date": "2026-03-04", "home_team": "DEN", "away_team": "LAL",
         "home_score": 121, "away_score": 117, "status": "final", "arena": "Ball Arena", "period": None,
         "game_clock": None, "start_time_et": "21:00"}]},
})

# (url, service module, class, method, envelope, cache)
GAME_DETAIL = GameDetailResp.model_validate({
    "status": "success", "message": "LAL at DEN, 2026-03-02",
    "data": {
        "game": {"game_id": "0022500900", "game_date": "2026-03-02", "home_team": "DEN", "away_team": "LAL",
                 "home_score": 121, "away_score": 117, "status": "final", "arena": "Ball Arena", "period": 4,
                 "game_clock": None, "start_time_et": "21:00"},
        "home_periods": [30, 28, 33, 30], "away_periods": [27, 31, 29, 30],
        "score_history": [{"captured_at": "2026-03-03T02:11:01.123456", "period": 1, "game_clock": "PT07M20.00S",
                           "home_score": 9, "away_score": 8, "game_status": 2}],
        "lines": [{"player_id": 203999, "espn_id": 3112335, "player_name": "Nikola Jokić", "team": "DEN", "min": 37,
                   "fpts": 70, "pts": 31, "reb": 14, "ast": 11, "stl": 2, "blk": 1, "tov": 4, "fgm": 12, "fga": 19,
                   "fg3m": 2, "fg3a": 4, "ftm": 5, "fta": 6, "plus_minus": None}],
        "lines_source": "settled",
    },
})


def game_detail(**game) -> GameDetailResp:
    """GAME_DETAIL with its game (and, for a period line, `home_periods`/`away_periods`) changed."""
    periods = {side: game.pop(side, getattr(GAME_DETAIL.data, side)) for side in ("home_periods", "away_periods")}
    data = GAME_DETAIL.data.model_copy(update={"game": GAME_DETAIL.data.game.model_copy(update=game), **periods})
    return GAME_DETAIL.model_copy(update={"data": data})


ROUTES = [
    ("/v1/players/203999/profile", "player_profile_service", "PlayerProfileService", "get_profile", PROFILE, "PLAYER_PROFILE"),
    ("/v1/players/203999/projection", "public_market_service", "PublicMarketService", "get_projection", PROJECTION, "PLAYER_PROJECTION"),
    ("/v1/players/stats?player_id=203999", "player_service", "PlayerService", "get_player_stats", STATS, "PLAYER_STATS"),
    ("/v1/players/203999/stats?window=l5", "player_service", "PlayerService", "get_player_stats", STATS, "PLAYER_STATS"),
    ("/v1/players/203999/trends", "trends_service", "TrendsService", "get_player_trends", TRENDS, "PLAYER_TRENDS"),
    ("/v1/players/203999/percentiles", "player_service", "PlayerService", "get_player_percentiles", PERCENTILES, "PLAYER_PERCENTILES"),
    ("/v1/teams/DEN/schedule?limit=82", "team_schedule_service", "TeamScheduleService", "get_team_schedule", SCHEDULE, "TEAM_SCHEDULE"),
    ("/v1/teams/DEN/stats", "nba_team_stats_service", "NBATeamStatsService", "get_team_stats", TEAM_STATS, "TEAM_STATS"),
    ("/v1/teams/DEN/roster", "nba_team_roster_service", "NBATeamRosterService", "get_team_roster", ROSTER, "TEAM_ROSTER"),
    ("/v1/games/2026-03-04", "games_service", "GamesService", "get_games_on_date", GAMES, "GAMES_SETTLED"),
    ("/v1/games/2026-03-02/0022500900", "game_detail_service", "GameDetailService", "get_game", GAME_DETAIL, "GAME_DETAIL_SETTLED"),
]
ROUTE_IDS = [r[0] for r in ROUTES]


def stub(monkeypatch, module: str, cls: str, method: str, response) -> AsyncMock:
    mock = AsyncMock(return_value=response)
    monkeypatch.setattr(getattr(import_module(f"services.{module}"), cls), method, mock)
    return mock


def max_age(res) -> int:
    directive, _, value = res.headers["cache-control"].partition(", max-age=")
    assert directive == "public"
    return int(value)


@pytest.fixture
def frozen_nba_date(monkeypatch):
    """Pin the routes' NBA date (6 AM ET rule) without freezing the event loop's clock."""
    from api.v1.public import games, teams

    def pin(day: date):
        monkeypatch.setattr(games, "nba_date_et", lambda: day)
        monkeypatch.setattr(teams, "nba_date_et", lambda: day)
    pin(date(2026, 10, 13))
    return pin


@pytest.fixture(autouse=True)
def pool_clock(monkeypatch):
    """The percentile pool's monotonic clock, moved only by hand. The percentile
    route keys its entries by the pool's ten-minute window, so on the real clock
    a MISS-then-HIT pair could straddle a window's end; this one starts at the
    beginning of a window and stays there."""
    from services import player_service

    clock = SimpleNamespace(now=6000.0)
    monkeypatch.setattr(player_service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


# ---- the contract, route by route -------------------------------------------


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_cached_body_is_byte_identical_to_the_response_model_rendering(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    stub(monkeypatch, module, cls, method, response)
    target = getattr(caches, cache)
    ttl = target.ttl_seconds

    monkeypatch.setattr(target, "ttl_seconds", 0.0)   # kill switch: FastAPI renders, as before
    uncached = client.get(url)
    monkeypatch.setattr(target, "ttl_seconds", ttl)
    miss = client.get(url)
    hit = client.get(url)

    assert uncached.status_code == miss.status_code == hit.status_code == 200
    assert uncached.content == miss.content == hit.content
    assert uncached.headers["content-type"] == miss.headers["content-type"] == "application/json"
    assert "cache-control" not in uncached.headers and "etag" not in uncached.headers


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_a_hit_skips_the_service_and_says_how_long_to_keep_it(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    service = stub(monkeypatch, module, cls, method, response)

    first, second = client.get(url), client.get(url)

    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert service.await_count == 1
    assert first.headers["etag"] == second.headers["etag"]
    assert 0 < max_age(second) <= getattr(caches, cache).ttl_seconds


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_etag_round_trip_answers_304_without_a_body(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    service = stub(monkeypatch, module, cls, method, response)
    etag = client.get(url).headers["etag"]

    conditional = client.get(url, headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == etag
    assert max_age(conditional) > 0
    assert client.get(url, headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    assert client.get(url, headers={"If-None-Match": '"someone-elses"'}).status_code == 200
    assert service.await_count == 1


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_an_error_envelope_is_never_cached(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    service = stub(monkeypatch, module, cls, method,
                   BaseResponse(status=ApiStatus.NOT_FOUND, message="nothing here"))

    first, second = client.get(url), client.get(url)

    assert first.status_code == second.status_code == 404
    assert service.await_count == 2
    assert not {"cache-control", "etag", "x-cache"} & set(first.headers)
    assert getattr(caches, cache).stats()["entries"] == 0


@pytest.mark.parametrize("url,module,cls,method", [
    ("/v1/players/stats?player_id=1", "player_service", "PlayerService", "get_player_stats"),
    ("/v1/players/1/percentiles", "player_service", "PlayerService", "get_player_percentiles"),
    ("/v1/players/1/projection", "public_market_service", "PublicMarketService", "get_projection"),
])
def test_a_raised_not_found_is_never_cached(client, monkeypatch, url, module, cls, method):
    service = AsyncMock(side_effect=NotFoundError("PLAYER_NOT_FOUND", "Player not found"))
    monkeypatch.setattr(getattr(import_module(f"services.{module}"), cls), method, service)

    first, second = client.get(url), client.get(url)

    assert first.status_code == second.status_code == 404
    assert first.json()["error_code"] == "PLAYER_NOT_FOUND"
    assert service.await_count == 2
    assert "cache-control" not in first.headers


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_a_database_error_is_a_503_and_caches_nothing(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    """A service that re-raises a dropped connection (the team schedule's rating
    lookup does, rather than render blank ratings) leaves the cache empty, so
    the next request renders afresh."""
    service = AsyncMock(side_effect=[OperationalError("server closed the connection unexpectedly"), response])
    monkeypatch.setattr(getattr(import_module(f"services.{module}"), cls), method, service)

    failed = client.get(url)
    assert failed.status_code == 503
    assert failed.json()["error_code"] == "DATABASE_UNAVAILABLE"
    assert "cache-control" not in failed.headers
    assert getattr(caches, cache).stats()["entries"] == 0

    recovered = client.get(url)
    assert (recovered.status_code, recovered.headers["x-cache"]) == (200, "MISS")
    assert service.await_count == 2


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_the_kill_switch_restores_the_uncached_route(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    service = stub(monkeypatch, module, cls, method, response)
    monkeypatch.setattr(getattr(caches, cache), "ttl_seconds", 0.0)

    first, second = client.get(url), client.get(url)

    assert first.status_code == second.status_code == 200
    assert service.await_count == 2
    assert not {"cache-control", "etag", "x-cache"} & set(first.headers)


def vary(res) -> set[str]:
    return {field.strip().lower() for field in res.headers.get("vary", "").split(",") if field.strip()}


def test_a_cross_origin_copy_varies_on_the_origin(client, monkeypatch):
    """Cache-Control is public, and CORS echoes the caller's origin: a shared
    cache must key on Origin, which Starlette's CORSMiddleware says with Vary."""
    stub(monkeypatch, "player_profile_service", "PlayerProfileService", "get_profile", PROFILE)

    res = client.get("/v1/players/203999/profile", headers={"Origin": "https://www.courtvision.dev"})

    assert res.headers["access-control-allow-origin"] == "https://www.courtvision.dev"
    assert vary(res) == {"origin"}
    assert "etag" in res.headers["access-control-expose-headers"].lower()
    assert max_age(res) > 0


@pytest.mark.parametrize("url,module,cls,method,response,cache", ROUTES, ids=ROUTE_IDS)
def test_a_copy_fetched_without_an_origin_varies_on_it_too(
    client, monkeypatch, frozen_nba_date, url, module, cls, method, response, cache,
):
    """A same-origin GET (Swagger's "Try it out", the URL opened in a tab) sends
    no Origin, so CORSMiddleware adds neither allow-origin nor Vary. Without a
    Vary of its own, that copy would answer the app's CORS fetch of the same URL
    from the browser cache, with no allow-origin header, and fail it."""
    stub(monkeypatch, module, cls, method, response)

    miss = client.get(url)
    hit = client.get(url)
    not_modified = client.get(url, headers={"If-None-Match": hit.headers["etag"]})
    cross_origin = client.get(url, headers={"Origin": "https://www.courtvision.dev"})

    assert [r.headers["x-cache"] for r in (miss, hit, not_modified)] == ["MISS", "HIT", "HIT"]
    assert not_modified.status_code == 304
    for res in (miss, hit, not_modified):
        assert "access-control-allow-origin" not in res.headers
        assert vary(res) == {"origin"}
    # The middleware's own Vary is appended to this one, not lost or replacing it.
    assert cross_origin.headers["access-control-allow-origin"] == "https://www.courtvision.dev"
    assert vary(cross_origin) == {"origin"}


# ---- keys: every input that changes a body ----------------------------------


def misses(client, urls) -> list[str]:
    return [client.get(u).headers["x-cache"] for u in urls]


def test_stats_keys_separate_every_lookup_parameter(client, monkeypatch):
    service = stub(monkeypatch, "player_service", "PlayerService", "get_player_stats", STATS)
    urls = [
        "/v1/players/stats?player_id=203999",
        "/v1/players/stats?espn_id=203999",             # the same number in the other id system
        "/v1/players/stats?player_id=203999&window=l10",
        "/v1/players/stats?player_id=203999&team=DEN",
        "/v1/players/stats?name=Nikola%20Joki%C4%87",
        "/v1/players/stats?name=Nikola%20Joki%C4%87&team=DEN",
        "/v1/players/stats?espn_id=3112335&player_id=203999",
    ]

    assert misses(client, urls) == ["MISS"] * len(urls)
    assert misses(client, urls) == ["HIT"] * len(urls)
    assert service.await_count == len(urls)


def test_the_stats_path_route_shares_the_query_routes_entry(client, monkeypatch):
    service = stub(monkeypatch, "player_service", "PlayerService", "get_player_stats", STATS)

    assert client.get("/v1/players/stats?player_id=203999&window=l5").headers["x-cache"] == "MISS"
    assert client.get("/v1/players/203999/stats?window=l5").headers["x-cache"] == "HIT"
    assert client.get("/v1/players/203999/stats").headers["x-cache"] == "MISS"   # window=season
    service.assert_any_await(espn_id=None, player_id=203999, name=None, team=None, window="l5")
    assert service.await_count == 2


def test_projection_keys_resolve_the_default_season_and_keep_as_of(client, monkeypatch):
    service = stub(monkeypatch, "public_market_service", "PublicMarketService", "get_projection", PROJECTION)
    monkeypatch.setattr(settings, "nba_season", "2025-26")

    assert client.get("/v1/players/1/projection").headers["x-cache"] == "MISS"
    assert client.get("/v1/players/1/projection?season=2025-26").headers["x-cache"] == "HIT"
    assert client.get("/v1/players/1/projection?season=2026-27").headers["x-cache"] == "MISS"
    assert client.get("/v1/players/1/projection?as_of=2026-09-03").headers["x-cache"] == "MISS"
    assert client.get("/v1/players/2/projection").headers["x-cache"] == "MISS"
    assert service.await_count == 4


def test_percentile_keys_separate_players_and_floors(client, monkeypatch):
    service = stub(monkeypatch, "player_service", "PlayerService", "get_player_percentiles", PERCENTILES)
    urls = ["/v1/players/1/percentiles", "/v1/players/1/percentiles?min_games=5", "/v1/players/2/percentiles"]

    assert misses(client, urls) == ["MISS"] * 3
    assert client.get("/v1/players/1/percentiles?min_games=20").headers["x-cache"] == "HIT"   # the default
    assert service.await_count == 3


def test_a_percentile_body_never_outlives_the_pool_it_was_built_from(client, monkeypatch, pool_clock):
    """Percentiles are read off the league pool, held for one ten-minute window.
    A body built from a pool with ten seconds left is advertised for those ten
    seconds at most and is not served once that pool is replaced, so percentiles
    trail a pipeline write by the pool's ten minutes, never ten plus the TTL's five."""
    from services import player_service

    pools: list[float] = []

    async def service(player_id: int, min_games: int = 20) -> PlayerPercentilesResp:
        # The real service's pool cache, over a stand-in aggregate that counts its builds.
        (built,) = player_service._cached_percentile_pool(lambda: pools.append(pool_clock.now) or [len(pools)])
        return PERCENTILES.model_copy(update={"message": f"pool {built}"})

    monkeypatch.setattr(player_service.PlayerService, "get_player_percentiles", service)

    assert client.get("/v1/players/1/percentiles").json()["message"] == "pool 1"
    pool_clock.now += 590
    late = client.get("/v1/players/2/percentiles")
    assert (late.headers["x-cache"], late.json()["message"]) == ("MISS", "pool 1")
    assert max_age(late) == 10

    pool_clock.now += 20   # pool 1 was replaced at the window's end, ten seconds ago
    for url in ("/v1/players/1/percentiles", "/v1/players/2/percentiles"):
        again = client.get(url)
        assert (again.headers["x-cache"], again.json()["message"]) == ("MISS", "pool 2")
        assert 295 < max_age(again) <= 300   # the entry's own five minutes: the new window has 580 s left
    assert pools == [6000.0, 6610.0]


def test_team_schedule_keys_separate_upcoming_limit_and_the_nba_date(client, monkeypatch, frozen_nba_date):
    service = stub(monkeypatch, "team_schedule_service", "TeamScheduleService", "get_team_schedule", SCHEDULE)
    urls = ["/v1/teams/DEN/schedule", "/v1/teams/DEN/schedule?limit=82",
            "/v1/teams/DEN/schedule?upcoming=true&limit=7", "/v1/teams/LAL/schedule"]

    assert misses(client, urls) == ["MISS"] * 4
    # The service upper-cases the abbreviation first, so the key does too.
    assert client.get("/v1/teams/den/schedule").headers["x-cache"] == "HIT"
    # A new NBA date moves where an upcoming list starts; the full list is unaffected.
    frozen_nba_date(date(2026, 10, 14))
    assert client.get("/v1/teams/DEN/schedule?upcoming=true&limit=7").headers["x-cache"] == "MISS"
    assert client.get("/v1/teams/DEN/schedule").headers["x-cache"] == "HIT"
    assert service.await_count == 5


@pytest.mark.parametrize("path,module,cls,method,response", [
    ("stats", "nba_team_stats_service", "NBATeamStatsService", "get_team_stats", TEAM_STATS),
    ("roster", "nba_team_roster_service", "NBATeamRosterService", "get_team_roster", ROSTER),
])
def test_team_sheet_keys_are_per_team_whatever_the_case(client, monkeypatch, path, module, cls, method, response):
    service = stub(monkeypatch, module, cls, method, response)

    assert client.get(f"/v1/teams/DEN/{path}").headers["x-cache"] == "MISS"
    assert client.get(f"/v1/teams/den/{path}").headers["x-cache"] == "HIT"
    assert client.get(f"/v1/teams/LAL/{path}").headers["x-cache"] == "MISS"
    assert service.await_count == 2


def test_another_season_is_another_entry(client, monkeypatch):
    service = stub(monkeypatch, "player_service", "PlayerService", "get_player_stats", STATS)

    assert client.get("/v1/players/stats?player_id=1").headers["x-cache"] == "MISS"
    monkeypatch.setattr(settings, "nba_season", "2026-27")
    assert client.get("/v1/players/stats?player_id=1").headers["x-cache"] == "MISS"
    assert service.await_count == 2


# ---- games: today is live, the past is settled ------------------------------


def test_todays_games_are_never_cached(client, monkeypatch, frozen_nba_date):
    service = stub(monkeypatch, "games_service", "GamesService", "get_games_on_date", GAMES)
    frozen_nba_date(date(2026, 3, 4))

    first, second = client.get("/v1/games/2026-03-04"), client.get("/v1/games/2026-03-04")

    assert first.status_code == second.status_code == 200
    assert service.await_count == 2
    assert not {"cache-control", "etag", "x-cache"} & set(first.headers)
    assert caches.GAMES.stats()["entries"] == caches.GAMES_SETTLED.stats()["entries"] == 0


@pytest.mark.parametrize("day,cache", [
    ("2026-03-03", "GAMES"),           # last night: its final scores may still be landing
    ("2026-03-05", "GAMES"),           # tomorrow
    ("2026-04-20", "GAMES"),           # weeks ahead
    ("2026-03-02", "GAMES_SETTLED"),   # two nights back: final
    ("2013-01-15", "GAMES_SETTLED"),
])
def test_other_dates_are_cached_by_how_settled_they_are(client, monkeypatch, frozen_nba_date, day, cache):
    stub(monkeypatch, "games_service", "GamesService", "get_games_on_date", GAMES)
    frozen_nba_date(date(2026, 3, 4))

    res = client.get(f"/v1/games/{day}")

    assert res.headers["x-cache"] == "MISS"
    ttl = getattr(caches, cache).ttl_seconds
    assert ttl - 5 < max_age(res) <= ttl   # the entry's remaining life, truncated
    assert getattr(caches, cache).stats()["entries"] == 1


def test_the_settled_ttl_is_longer_than_the_daily_one():
    assert caches.GAMES_SETTLED.ttl_seconds > caches.GAMES.ttl_seconds == caches.DAILY_TTL_SECONDS


def test_a_cached_date_is_not_served_once_it_becomes_today(client, monkeypatch, frozen_nba_date):
    service = stub(monkeypatch, "games_service", "GamesService", "get_games_on_date", GAMES)
    frozen_nba_date(date(2026, 3, 3))
    assert client.get("/v1/games/2026-03-04").headers["x-cache"] == "MISS"   # tomorrow, cached

    frozen_nba_date(date(2026, 3, 4))                                          # the 6 AM ET flip
    today = client.get("/v1/games/2026-03-04")

    assert "x-cache" not in today.headers
    assert service.await_count == 2


# ---- one game: kept once it is over and whole -------------------------------


@pytest.mark.parametrize("day,cache", [
    ("2026-03-04", "GAME_DETAIL"),          # tonight, already final
    ("2026-03-03", "GAME_DETAIL"),          # last night: its game log may still be landing
    ("2026-03-02", "GAME_DETAIL_SETTLED"),  # two nights back
    ("2013-01-15", "GAME_DETAIL_SETTLED"),
])
def test_a_final_game_is_cached_by_how_settled_its_night_is(client, monkeypatch, frozen_nba_date, day, cache):
    stub(monkeypatch, "game_detail_service", "GameDetailService", "get_game", GAME_DETAIL)
    frozen_nba_date(date(2026, 3, 4))

    res = client.get(f"/v1/games/{day}/0022500900")

    assert res.headers["x-cache"] == "MISS"
    assert getattr(caches, cache).stats()["entries"] == 1


@pytest.mark.parametrize("response", [
    game_detail(status="in_progress", period=3, game_clock="PT04M10.00S"),
    game_detail(status="scheduled", home_score=None, away_score=None),
    game_detail(home_periods=[]),   # final, but the league feed gave no period line: ask again
    game_detail(away_periods=[]),   # or gave only one side's
], ids=["in-progress", "scheduled", "final-without-periods", "final-with-one-sides-periods"])
def test_a_game_still_moving_or_missing_its_period_line_is_not_cached(client, monkeypatch, frozen_nba_date, response):
    service = stub(monkeypatch, "game_detail_service", "GameDetailService", "get_game", response)
    frozen_nba_date(date(2026, 3, 4))

    first, second = client.get("/v1/games/2026-03-04/0022500900"), client.get("/v1/games/2026-03-04/0022500900")

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert service.await_count == 2
    assert not {"cache-control", "etag", "x-cache"} & set(first.headers)
    assert caches.GAME_DETAIL.stats()["entries"] == caches.GAME_DETAIL_SETTLED.stats()["entries"] == 0


def test_game_keys_separate_games_and_nights(client, monkeypatch, frozen_nba_date):
    service = stub(monkeypatch, "game_detail_service", "GameDetailService", "get_game", GAME_DETAIL)

    for url in ("/v1/games/2026-03-02/0022500900", "/v1/games/2026-03-02/0022500901", "/v1/games/2026-03-01/0022500900"):
        assert client.get(url).headers["x-cache"] == "MISS"
    assert client.get("/v1/games/2026-03-02/0022500900").headers["x-cache"] == "HIT"
    assert service.await_count == 3


# ---- what stays uncached ----------------------------------------------------


@pytest.mark.parametrize("url,module,cls,method,response", [
    ("/v1/players/1/status", "player_service", "PlayerService", "get_player_status",
     PlayerStatusResp(status=ApiStatus.SUCCESS, message="No current injury report", data=None)),
    ("/v1/players/1/ownership", "ownership_service", "OwnershipService", "get_player_ownership",
     PlayerOwnershipResp(status=ApiStatus.SUCCESS, message="ok", data=None)),
    ("/v1/teams/DEN/live-game", "nba_team_live_game_service", "NBATeamLiveGameService", "get_live_game",
     NBATeamLiveGameResp(status=ApiStatus.SUCCESS, message="ok", data=None)),
])
def test_injury_status_ownership_and_live_reads_stay_uncached(client, monkeypatch, url, module, cls, method, response):
    service = stub(monkeypatch, module, cls, method, response)

    first, second = client.get(url), client.get(url)

    assert first.status_code == second.status_code == 200
    assert service.await_count == 2
    assert not {"cache-control", "etag", "x-cache"} & set(first.headers)


# ---- schedule weeks: the static calendar plus the fantasy day ----------------


@pytest.fixture
def season_25_26(monkeypatch):
    from services import schedule_service as ss

    monkeypatch.setattr(settings, "nba_season", "2025-26")
    ss.reset_cache()
    yield
    ss.reset_cache()


@pytest.fixture
def fantasy_day(monkeypatch):
    from api.v1.public import schedule

    def pin(day: date, seconds_left: int = 3 * 3600):
        monkeypatch.setattr(schedule, "get_nba_today", lambda: day)
        monkeypatch.setattr(schedule, "seconds_left_in_fantasy_day", lambda d: seconds_left)
    return pin


def test_schedule_weeks_are_cached_byte_identically(client, monkeypatch, season_25_26, fantasy_day):
    fantasy_day(date(2025, 11, 5))
    monkeypatch.setattr(caches.SCHEDULE_WEEKS, "ttl_seconds", 0.0)
    uncached = client.get("/v1/schedule/weeks")
    monkeypatch.setattr(caches.SCHEDULE_WEEKS, "ttl_seconds", caches.SETTLED_TTL_SECONDS)
    miss, hit = client.get("/v1/schedule/weeks"), client.get("/v1/schedule/weeks")

    assert uncached.content == miss.content == hit.content
    assert (miss.headers["x-cache"], hit.headers["x-cache"]) == ("MISS", "HIT")
    data = hit.json()["data"]
    assert data["current_week"] == 3 and data["season"]["season_day"] == 16 and data["season"]["phase"] == "regular"


def test_schedule_weeks_are_never_kept_past_the_fantasy_rollover(client, season_25_26, fantasy_day):
    fantasy_day(date(2025, 11, 5), seconds_left=600)   # 1:50 AM ET

    assert max_age(client.get("/v1/schedule/weeks")) == 600
    fantasy_day(date(2025, 11, 5), seconds_left=86_000)
    assert max_age(client.get("/v1/schedule/weeks")) <= caches.SETTLED_TTL_SECONDS


def test_a_new_fantasy_day_builds_a_new_body(client, season_25_26, fantasy_day):
    fantasy_day(date(2025, 11, 2))   # a Sunday: week 2's last day
    sunday = client.get("/v1/schedule/weeks")
    fantasy_day(date(2025, 11, 3))   # the 2 AM ET rollover into Monday
    monday = client.get("/v1/schedule/weeks")

    assert (sunday.headers["x-cache"], monday.headers["x-cache"]) == ("MISS", "MISS")
    assert sunday.json()["data"]["current_week"] == 2
    assert monday.json()["data"]["current_week"] == 3
    assert monday.json()["data"]["season"]["season_day"] == 14
