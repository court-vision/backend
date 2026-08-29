"""
API tests for GET /v1/internal/rankings/{team_id}

Covers ownership, the scoring resolved from the auth context, and the cache —
which is keyed by league scoring rather than by user, so two teams in one
league must share an entry and a settings change must not.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from schemas.rankings import RankingsMeta, RankingsPlayer, RankingsResp, RankingsScoring

NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in ("fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov")
]

RESP = RankingsResp(
    status=ApiStatus.SUCCESS,
    message="Rankings fetched successfully",
    data=[RankingsPlayer(id=1, rank=1, player_name="Nikola Jokić", team="DEN",
                         total_fpts=2500.0, avg_fpts=62.5)],
    meta=RankingsMeta(format="points", pool_size=1,
                      scoring=RankingsScoring(basis="league_points", point_weights={"pts": 2.0})),
)


def _league(**overrides):
    base = dict(
        id=3, provider="espn", provider_league_id="993431466", season=2027, name="Dunk Dynasty",
        scoring_type="points", category_win_mode=None, categories=[], point_weights={"pts": 2.0},
        matchup_periods={}, roster_slots={}, raw_settings={"_sync": {"unsupported": [], "warnings": []}},
        settings_synced_at=datetime(2026, 8, 24, 12, 0, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _own(monkeypatch, team_id=7, league=None, league_id=3):
    """Make `team_id` owned by the fake user, carrying `league`."""
    from api import deps
    from services import user_sync_service

    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: SimpleNamespace(user_id=42)))
    team = SimpleNamespace(team_id=team_id, user_id_id=42, league_info="{}",
                           league_id=league_id, league=league)
    monkeypatch.setattr(deps, "ensure_team_owned", lambda tid, uid: team if tid == team_id else None)
    return team


@pytest.fixture
def service(monkeypatch):
    """Stub the service; record the ResolvedScoring and params it was handed."""
    from services import rankings_service

    state = {"calls": []}

    async def fake(scoring, window=None, min_games=None):
        state["calls"].append({"scoring": scoring, "window": window, "min_games": min_games})
        return RESP

    monkeypatch.setattr(rankings_service.RankingsService, "get_league_rankings", staticmethod(fake))
    return state


@pytest.mark.api
def test_a_team_you_do_not_own_is_a_404(authed_client, service, monkeypatch):
    _own(monkeypatch, team_id=7, league=_league())

    res = authed_client.get("/v1/internal/rankings/999")

    assert res.status_code == 404
    assert res.json()["error_code"] == "TEAM_NOT_FOUND"
    assert service["calls"] == []


@pytest.mark.api
def test_the_league_from_the_auth_context_becomes_the_scoring(authed_client, service, monkeypatch):
    """No second database trip: `get_owned_team` already loaded the league."""
    _own(monkeypatch, league=_league(point_weights={"pts": 2.0, "reb": 3.0}))

    res = authed_client.get("/v1/internal/rankings/7")

    assert res.status_code == 200
    scoring = service["calls"][0]["scoring"]
    assert scoring.format == "points"
    assert scoring.points.weights == {"pts": 2.0, "reb": 3.0}
    assert scoring.settings_synced is True


@pytest.mark.api
def test_a_category_league_resolves_to_its_own_categories(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league(scoring_type="categories", categories=NINE_CAT,
                                     category_win_mode="each_category"))

    res = authed_client.get("/v1/internal/rankings/7")

    assert res.status_code == 200
    scoring = service["calls"][0]["scoring"]
    assert scoring.is_categories
    assert scoring.categories.keys == [c["key"] for c in NINE_CAT]


@pytest.mark.api
def test_window_and_min_games_pass_through(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league())

    assert authed_client.get("/v1/internal/rankings/7?window=14&min_games=5").status_code == 200
    assert service["calls"][0]["window"] == 14
    assert service["calls"][0]["min_games"] == 5


@pytest.mark.api
@pytest.mark.parametrize("query", ["window=12", "min_games=0", "min_games=99"])
def test_invalid_params_are_422(authed_client, service, monkeypatch, query):
    _own(monkeypatch, league=_league())

    assert authed_client.get(f"/v1/internal/rankings/7?{query}").status_code == 422
    assert service["calls"] == []


@pytest.mark.api
def test_the_response_is_cached_privately(authed_client, service, monkeypatch):
    """A shared cache must never be told it may hold an authenticated response."""
    _own(monkeypatch, league=_league())

    first = authed_client.get("/v1/internal/rankings/7")
    second = authed_client.get("/v1/internal/rankings/7")

    assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
    assert first.headers["cache-control"].startswith("private, ")
    assert first.content == second.content
    assert len(service["calls"]) == 1


@pytest.mark.api
def test_two_teams_in_one_league_share_a_cached_response(authed_client, service, monkeypatch):
    """The key is the league's scoring, not the caller — nothing in the body is per-user."""
    from api import deps
    from services import user_sync_service

    league = _league()
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: SimpleNamespace(user_id=42)))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda tid, uid: SimpleNamespace(
        team_id=tid, user_id_id=42, league_info="{}", league_id=league.id, league=league))

    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "MISS"
    assert authed_client.get("/v1/internal/rankings/8").headers["x-cache"] == "HIT"
    assert len(service["calls"]) == 1


@pytest.mark.api
def test_changed_league_settings_do_not_serve_the_old_numbers(authed_client, service, monkeypatch):
    """The fingerprint is in the key, so a re-sync invalidates without being told to."""
    _own(monkeypatch, league=_league(point_weights={"pts": 1.0}))
    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "MISS"
    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "HIT"

    _own(monkeypatch, league=_league(point_weights={"pts": 3.0}))     # settings re-synced
    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "MISS"
    assert len(service["calls"]) == 2


@pytest.mark.api
def test_different_leagues_do_not_share_an_entry(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league(id=3), league_id=3)
    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "MISS"

    _own(monkeypatch, league=_league(id=4, name="Other"), league_id=4)
    assert authed_client.get("/v1/internal/rankings/7").headers["x-cache"] == "MISS"


@pytest.mark.api
def test_a_conditional_request_is_answered_304(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league())
    etag = authed_client.get("/v1/internal/rankings/7").headers["etag"]

    conditional = authed_client.get("/v1/internal/rankings/7", headers={"If-None-Match": etag})

    assert conditional.status_code == 304 and conditional.content == b""


@pytest.mark.api
def test_a_team_with_no_league_still_ranks(authed_client, service, monkeypatch):
    """Nothing to score by yet is the platform default, not an error."""
    _own(monkeypatch, league=None, league_id=None)

    res = authed_client.get("/v1/internal/rankings/7")

    assert res.status_code == 200
    assert service["calls"][0]["scoring"].league is None
