"""
API tests for GET /v1/internal/drafts/board

Covers ownership, the scoring resolved from the auth context, and the
picked/mine query params reaching the service as id sets.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from schemas.draft import DraftBoardMeta, DraftBoardResp, DraftBoardRow

NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in ("fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov")
]

RESP = DraftBoardResp(
    status=ApiStatus.SUCCESS,
    message="Draft board fetched successfully (1 available of 1)",
    data=[DraftBoardRow(player_id=203999, espn_id=3112335, name="Nikola Jokić", team="DEN",
                        position="C", cv_rank=1, value=62.5, value_source="baseline",
                        last_season_gp=70, fpts_avg=62.5, market_rank=1, market_delta=0)],
    meta=DraftBoardMeta(season="2026-27", format="points", value_kind="fpts",
                        pool_size=1, available=1, projection_count=0, baseline_count=1),
)


def _league(**overrides):
    base = dict(
        id=3, provider="espn", provider_league_id="993431466", season=2027, name="Dunk Dynasty",
        scoring_type="points", category_win_mode=None, categories=[], point_weights={"pts": 2.0},
        matchup_periods={}, roster_slots={}, position_limits={}, draft_settings={},
        raw_settings={"_sync": {"unsupported": [], "warnings": []}},
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
    """Stub the service; record the ResolvedScoring and id sets it was handed."""
    from services import draft_board_service

    state = {"calls": []}

    async def fake(scoring, picked_ids=(), my_ids=()):
        state["calls"].append({"scoring": scoring,
                               "picked": set(picked_ids), "mine": set(my_ids)})
        return RESP

    monkeypatch.setattr(draft_board_service.DraftBoardService, "get_board", staticmethod(fake))
    return state


@pytest.mark.api
def test_a_team_you_do_not_own_is_a_404(authed_client, service, monkeypatch):
    _own(monkeypatch, team_id=7, league=_league())

    res = authed_client.get("/v1/internal/drafts/board?team_id=999")

    assert res.status_code == 404
    assert res.json()["error_code"] == "TEAM_NOT_FOUND"
    assert service["calls"] == []


@pytest.mark.api
def test_the_league_from_the_auth_context_becomes_the_scoring(authed_client, service, monkeypatch):
    """No second database trip: `get_owned_team` already loaded the league."""
    _own(monkeypatch, league=_league(point_weights={"pts": 2.0, "reb": 3.0}))

    res = authed_client.get("/v1/internal/drafts/board?team_id=7")

    assert res.status_code == 200
    body = res.json()
    assert body["data"][0]["player_id"] == 203999 and body["meta"]["value_kind"] == "fpts"
    scoring = service["calls"][0]["scoring"]
    assert scoring.format == "points"
    assert scoring.points.weights == {"pts": 2.0, "reb": 3.0}
    assert scoring.settings_synced is True


@pytest.mark.api
def test_a_category_league_resolves_to_its_own_categories(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league(scoring_type="categories", categories=NINE_CAT,
                                     category_win_mode="each_category"))

    res = authed_client.get("/v1/internal/drafts/board?team_id=7")

    assert res.status_code == 200
    scoring = service["calls"][0]["scoring"]
    assert scoring.is_categories
    assert scoring.categories.keys == [c["key"] for c in NINE_CAT]


@pytest.mark.api
def test_picked_and_mine_pass_through_as_id_sets(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league())

    res = authed_client.get(
        "/v1/internal/drafts/board?team_id=7&picked=201&picked=202&mine=203"
    )

    assert res.status_code == 200
    assert service["calls"][0]["picked"] == {201, 202}
    assert service["calls"][0]["mine"] == {203}


@pytest.mark.api
def test_both_default_to_empty(authed_client, service, monkeypatch):
    _own(monkeypatch, league=_league())

    assert authed_client.get("/v1/internal/drafts/board?team_id=7").status_code == 200
    assert service["calls"][0]["picked"] == set() and service["calls"][0]["mine"] == set()


@pytest.mark.api
@pytest.mark.parametrize("query", ["", "team_id=7&picked=jokic", "team_id=7&mine=1.5"])
def test_bad_params_are_422(authed_client, service, monkeypatch, query):
    _own(monkeypatch, league=_league())

    res = authed_client.get(f"/v1/internal/drafts/board?{query}")

    assert res.status_code == 422
    assert service["calls"] == []
