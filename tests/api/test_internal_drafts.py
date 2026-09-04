"""
API tests for the Draft Lab routes.

Covers the stateless board (ownership, the scoring resolved from the auth
context, picked/mine reaching the service as id sets), the session and pick
CRUD routes, and the session board — including that `/drafts/board` still wins
over `/drafts/{session_id}`.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from schemas.draft import DraftBoardMeta, DraftBoardResp, DraftBoardRow, DraftSessionResp

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

    async def fake(scoring, picked_ids=(), my_ids=(), session=None):
        state["calls"].append({"scoring": scoring, "picked": set(picked_ids),
                               "mine": set(my_ids), "session": session})
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


# ---- sessions and picks ----------------------------------------------------

SESSION = DraftSessionResp(
    id=12, team_id=7, league_id=3, kind="manual", status="active", draft_type="snake",
    pick_order=[10, 6, 5, 8, 2, 3, 7, 4, 9, 1], my_slot=3, rounds=13, keepers=[],
    league_size=10, keeper_count=0, total_picks=130, pick_count=0,
    next_overall_pick=1, my_next_pick=3, picks_until_my_turn=2,
)


def _own_session(monkeypatch, session_id=12, league=None, **overrides):
    """Make `session_id` owned by the fake user, carrying `league`."""
    from api import deps
    from services import user_sync_service

    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user",
                        staticmethod(lambda clerk_id, email: SimpleNamespace(user_id=42)))
    base = dict(
        session_id=session_id, user_id=42, team_id=7, league_id=3, league=league,
        kind="manual", status="active", draft_type="snake",
        pick_order=(10, 6, 5, 8, 2, 3, 7, 4, 9, 1), my_slot=3, rounds=13,
    )
    base.update(overrides)
    ctx = deps.OwnedDraftSessionContext(**base)
    monkeypatch.setattr(deps, "_owned_session", lambda sid, uid: ctx if sid == session_id else None)
    return ctx


@pytest.fixture
def draft_service(monkeypatch):
    """Stub every DraftService method; record what the routes handed it."""
    from services import draft_service as module

    calls = []

    def record(name, result):
        async def fake(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return staticmethod(fake)

    from schemas.draft import (
        DraftPickDeleteResponse, DraftPickResp, DraftPickResponse,
        DraftSessionDeleteResponse, DraftSessionListResponse, DraftSessionResponse,
    )

    created = DraftSessionResponse(status=ApiStatus.SUCCESS, message="Draft session created", data=SESSION)
    pick = DraftPickResponse(
        status=ApiStatus.SUCCESS, message="Pick 1 recorded",
        data=DraftPickResp(overall_pick=1, round=1, slot=1, player_id=203999, by_me=False, source="manual"),
    )
    for name, result in (
        ("create_session", created),
        ("get_session", created),
        ("update_session", created),
        ("list_sessions", DraftSessionListResponse(status=ApiStatus.SUCCESS, message="1 draft session fetched",
                                                   data=[SESSION])),
        ("add_pick", pick),
        ("remove_pick", DraftPickDeleteResponse(status=ApiStatus.SUCCESS, message="Pick 4 undone", data=4)),
        ("delete_session", DraftSessionDeleteResponse(status=ApiStatus.SUCCESS, message="Draft room #12 deleted", data=12)),
    ):
        monkeypatch.setattr(module.DraftService, name, record(name, result))
    return calls


@pytest.mark.api
def test_create_passes_the_body_through_with_the_callers_user_id(authed_client, draft_service, monkeypatch):
    _own(monkeypatch)   # only to install the fake user resolution

    res = authed_client.post("/v1/internal/drafts", json={"team_id": 7, "my_slot": 3})

    assert res.status_code == 200
    assert res.json()["data"]["id"] == 12
    name, args, _ = draft_service[0]
    assert name == "create_session" and args[0] == 42
    assert args[1].team_id == 7 and args[1].my_slot == 3 and args[1].kind == "manual"


@pytest.mark.api
def test_create_defaults_everything_the_league_can_prefill(authed_client, draft_service, monkeypatch):
    _own(monkeypatch)

    res = authed_client.post("/v1/internal/drafts", json={"team_id": 7})

    assert res.status_code == 200
    req = draft_service[0][1][1]
    assert req.draft_type is None and req.pick_order is None and req.rounds is None
    assert req.keepers == []


@pytest.mark.api
@pytest.mark.parametrize("body", [
    {"team_id": 0},                 # ge=1
    {"my_slot": 0},                 # ge=1
    {"kind": "shotgun"},            # not in the vocabulary
    {"rounds": 99},                 # le=40
])
def test_create_rejects_invalid_bodies(authed_client, draft_service, monkeypatch, body):
    _own(monkeypatch)
    assert authed_client.post("/v1/internal/drafts", json=body).status_code == 422
    assert draft_service == []


@pytest.mark.api
def test_list_is_scoped_to_the_caller(authed_client, draft_service, monkeypatch):
    _own(monkeypatch)

    res = authed_client.get("/v1/internal/drafts")

    assert res.status_code == 200 and res.json()["data"][0]["id"] == 12
    assert draft_service[0][0] == "list_sessions" and draft_service[0][1] == (42,)


@pytest.mark.api
def test_a_session_you_do_not_own_is_a_404(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch, session_id=12)

    res = authed_client.get("/v1/internal/drafts/999")

    assert res.status_code == 404
    assert res.json()["error_code"] == "DRAFT_SESSION_NOT_FOUND"
    assert draft_service == []


@pytest.mark.api
def test_get_and_patch_reach_the_service_with_the_owned_session_id(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)

    assert authed_client.get("/v1/internal/drafts/12").status_code == 200
    res = authed_client.patch("/v1/internal/drafts/12", json={"status": "completed"})

    assert res.status_code == 200
    assert draft_service[0][0] == "get_session" and draft_service[0][1] == (12,)
    name, args, _ = draft_service[1]
    assert name == "update_session" and args[0] == 12 and args[1].status == "completed"
    # An absent field is not "set to None": only what was sent is written.
    assert args[1].model_fields_set == {"status"}


@pytest.mark.api
def test_delete_reaches_the_service_with_the_owned_session_id(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)

    res = authed_client.delete("/v1/internal/drafts/12")

    assert res.status_code == 200
    assert res.json()["data"] == 12
    assert draft_service == [("delete_session", (12,), {})]


@pytest.mark.api
def test_deleting_a_session_you_do_not_own_is_a_404(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch, session_id=12)

    assert authed_client.delete("/v1/internal/drafts/13").status_code == 404
    assert draft_service == []


@pytest.mark.api
def test_a_live_room_without_a_team_is_a_422(authed_client, draft_service, monkeypatch):
    _own(monkeypatch)

    res = authed_client.post("/v1/internal/drafts", json={"kind": "live"})

    assert res.status_code == 422
    assert draft_service == []


@pytest.mark.api
def test_a_patch_can_rename_and_link_or_unlink(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)

    assert authed_client.patch("/v1/internal/drafts/12", json={"name": "  Tuesday practice "}).status_code == 200
    assert authed_client.patch("/v1/internal/drafts/12", json={"espn_league_id": 35392660}).status_code == 200
    assert authed_client.patch("/v1/internal/drafts/12", json={"espn_league_id": None}).status_code == 200

    sent = [args[1] for name, args, _ in draft_service if name == "update_session"]
    assert sent[0].model_fields_set == {"name"} and sent[0].name == "  Tuesday practice "
    assert sent[1].espn_league_id == 35392660
    # An explicit null is a field that was set — that is what unlinks.
    assert sent[2].model_fields_set == {"espn_league_id"} and sent[2].espn_league_id is None


@pytest.mark.api
def test_an_empty_patch_is_a_422(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)
    assert authed_client.patch("/v1/internal/drafts/12", json={}).status_code == 422
    assert draft_service == []


@pytest.mark.api
def test_a_pick_reaches_the_service_and_undo_takes_the_path_number(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)

    res = authed_client.post("/v1/internal/drafts/12/picks", json={"player_id": 203999, "by_me": True})
    assert res.status_code == 200 and res.json()["data"]["overall_pick"] == 1
    name, args, _ = draft_service[0]
    assert name == "add_pick" and args[0] == 12
    assert args[1].player_id == 203999 and args[1].by_me is True and args[1].overall_pick is None

    res = authed_client.delete("/v1/internal/drafts/12/picks/4")
    assert res.status_code == 200 and res.json()["data"] == 4
    assert draft_service[1][0] == "remove_pick" and draft_service[1][1] == (12, 4)


@pytest.mark.api
def test_a_pick_that_names_nobody_is_a_422(authed_client, draft_service, monkeypatch):
    _own_session(monkeypatch)
    assert authed_client.post("/v1/internal/drafts/12/picks", json={"by_me": True}).status_code == 422
    assert draft_service == []


@pytest.mark.api
def test_the_session_board_resolves_scoring_from_the_sessions_league(authed_client, service, monkeypatch):
    league = _league(point_weights={"pts": 2.0, "reb": 3.0})
    _own_session(monkeypatch, league=league)

    res = authed_client.get("/v1/internal/drafts/12/board")

    assert res.status_code == 200 and res.json()["data"][0]["player_id"] == 203999
    call = service["calls"][0]
    assert call["scoring"].points.weights == {"pts": 2.0, "reb": 3.0}
    # Pick state comes from the session, not the query string.
    assert call["picked"] == set() and call["mine"] == set()
    assert call["session"].session_id == 12 and call["session"].league_size == 10
    assert call["session"].my_slot == 3 and call["session"].rounds == 13


@pytest.mark.api
def test_a_board_for_a_session_you_do_not_own_is_a_404(authed_client, service, monkeypatch):
    _own_session(monkeypatch, session_id=12)
    res = authed_client.get("/v1/internal/drafts/999/board")
    assert res.status_code == 404 and res.json()["error_code"] == "DRAFT_SESSION_NOT_FOUND"
    assert service["calls"] == []


@pytest.mark.api
def test_the_literal_board_path_is_not_read_as_a_session_id(authed_client, service, monkeypatch):
    """`/drafts/board` must keep matching the stateless route, not `/{session_id}`."""
    _own(monkeypatch, league=_league())
    assert authed_client.get("/v1/internal/drafts/board?team_id=7").status_code == 200
    assert service["calls"][0]["session"] is None


# ------------------------------ INIT sync ------------------------------- #


@pytest.fixture
def draft_sync_service(monkeypatch):
    """Stub DraftSyncService.sync_init; record what the route handed it."""
    from services import draft_sync_service as module
    from schemas.draft import DraftInitSyncResp, DraftInitSyncResponse

    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return DraftInitSyncResponse(
            status=ApiStatus.SUCCESS,
            message="INIT reconciled: 45 inserted, 0 skipped",
            data=DraftInitSyncResp(
                session=SESSION, espn_league_id=588175580, espn_team_id=4, draft_state=1,
                draft_type="snake", made=45, inserted=45, skipped=0, conflicts=[], warnings=[],
                espn_front=46, header_applied=True, position_limits={"C": 4},
            ),
        )

    monkeypatch.setattr(module.DraftSyncService, "sync_init", staticmethod(fake))
    return calls


@pytest.mark.api
def test_sync_init_reaches_the_service_with_the_owned_session_id(authed_client, draft_sync_service, monkeypatch):
    _own_session(monkeypatch, session_id=12)

    res = authed_client.post("/v1/internal/drafts/12/sync/init", json={"payload": "A" * 40})

    assert res.status_code == 200
    body = res.json()["data"]
    assert body["inserted"] == 45 and body["espn_team_id"] == 4 and body["header_applied"] is True
    args, _ = draft_sync_service[0]
    assert args[0] == 12 and args[1].payload == "A" * 40


@pytest.mark.api
def test_sync_init_for_a_session_you_do_not_own_is_a_404(authed_client, draft_sync_service, monkeypatch):
    _own_session(monkeypatch, session_id=12)
    res = authed_client.post("/v1/internal/drafts/999/sync/init", json={"payload": "A" * 40})
    assert res.status_code == 404 and res.json()["error_code"] == "DRAFT_SESSION_NOT_FOUND"
    assert draft_sync_service == []


@pytest.mark.api
@pytest.mark.parametrize("body", [{}, {"payload": "short"}])
def test_sync_init_rejects_a_missing_or_tiny_payload(authed_client, draft_sync_service, monkeypatch, body):
    _own_session(monkeypatch, session_id=12)
    assert authed_client.post("/v1/internal/drafts/12/sync/init", json=body).status_code == 422
    assert draft_sync_service == []
