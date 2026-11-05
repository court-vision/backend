"""Dime's league tools (services/ai/league.py), through the agent's tool runner: who may
read what, and what the model gets to see. The league reads themselves are tested in
test_league_overview.py; the draft board in its own suites."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from core.errors import AppError
from schemas.common import ApiStatus
from schemas.draft import (
    CategoryNeedResp,
    DraftBoardMeta,
    DraftBoardResp,
    DraftBoardRow,
    DraftRecommendation,
    DraftRosterEntry,
    RecommendationComponent,
)
from services import league_overview
from services.ai import league, tools

pytestmark = pytest.mark.unit

CTX = tools.ToolContext(user_id=42, conversation_id=1, build=None)
TEAM = SimpleNamespace(team_id=21, user_id=42, league_id=7, league=None,
                       league_info_json=json.dumps({"provider": "espn", "league_id": 123, "team_name": "Atlas",
                                                    "year": 2027, "espn_team_id": 1}))


def _run(name, raw, ctx=CTX):
    return asyncio.run(tools.run_tool(name, raw, ctx=ctx, allowed=tools.AGENT_TOOL_NAMES))


async def _run_db(name, fn, *args, **kw):
    return fn(*args, **kw)


@pytest.fixture
def owned(monkeypatch):
    async def owned_team(ctx, team_id):
        if team_id != 21:
            raise AppError("TEAM_NOT_FOUND", f"Team {team_id} is not one of the user's teams")
        return TEAM, SimpleNamespace(provider="espn")

    monkeypatch.setattr(tools, "_owned", owned_team)
    monkeypatch.setattr(tools, "ensure_team_owned", lambda team_id, user_id: TEAM if team_id == 21 else None)
    monkeypatch.setattr(tools, "run_db", _run_db)
    monkeypatch.setattr(league, "run_db", _run_db)


def test_the_league_tools_follow_the_ledger_in_the_cached_prefix():
    names = [t["name"] for t in tools.AGENT_TOOLS]
    assert names[-4:] == ["get_move_outcomes", "get_league", "get_league_activity", "get_draft_board"]
    assert league.LEAGUE_TOOL_NAMES <= tools.AGENT_TOOL_NAMES
    assert not league.LEAGUE_TOOL_NAMES & (tools.ASK_TOOL_NAMES | tools.ROUTER_TOOL_NAMES)


# ---------------------------- get_league ---------------------------- #


def test_get_league_reads_an_owned_team_s_league(owned, monkeypatch):
    seen = {}

    async def read(league_info, **kw):
        seen.update(kw)
        return {"standings": [{"league_team_id": 1, "team_name": "Atlas", "mine": True}]}

    monkeypatch.setattr(league_overview, "read_league", read)
    out = _run("get_league", {"team_id": 21, "rosters": [1, 4], "date": "2026-11-02"})
    assert not out.is_error, out.content
    body = json.loads(out.content)
    assert body["team_id"] == 21 and body["standings"][0]["mine"] is True
    assert seen == {"roster_ids": [1, 4], "all_rosters": False, "day": date(2026, 11, 2)}


def test_get_league_refuses_someone_else_s_team_and_too_many_rosters(owned):
    out = _run("get_league", {"team_id": 99})
    assert out.is_error and "not one of the user's teams" in out.content
    out = _run("get_league", {"team_id": 21, "rosters": list(range(1, 30))})
    assert out.is_error and "Invalid input" in out.content


def test_a_league_read_refusal_reaches_the_model(owned, monkeypatch):
    async def read(league_info, **kw):
        raise league_overview.LeagueReadError("LEAGUE_ESPN_ONLY", "ESPN leagues only, and this is a Yahoo team")

    monkeypatch.setattr(league_overview, "read_league", read)
    out = _run("get_league", {"team_id": 21})
    assert out.is_error and "Yahoo team" in out.content


# ---------------------------- get_league_activity ---------------------------- #


def test_get_league_activity_reads_the_league_s_ledger_for_the_window(owned, monkeypatch):
    from services import move_ledger

    asked = []
    monkeypatch.setattr(move_ledger, "own_provider_team_id", lambda key, info: 1)
    monkeypatch.setattr("services.matchup_days.fantasy_today", lambda: date(2026, 11, 5))

    def read(q):
        asked.append(q)
        return {"since": q.since.isoformat(), "through": "2026-11-04", "total": 0, "by_team": [], "moves": []}

    monkeypatch.setattr(league_overview, "read_activity", read)
    out = _run("get_league_activity", {"team_id": 21, "days": 3, "league_team_id": 4, "player_id": 1002})
    assert not out.is_error, out.content
    body = json.loads(out.content)
    assert body["team_id"] == 21 and body["days"] == 3 and body["through"] == "2026-11-04"
    q = asked[0]
    assert (q.provider, q.provider_league_id, q.season, q.provider_team_id) == ("espn", "123", 2027, 1)
    assert q.since == date(2026, 11, 2) and q.league_team_id == 4 and q.nba_player_id == 1002 and q.limit == 25


def test_a_league_with_nothing_stored_says_so(owned, monkeypatch):
    from services import move_ledger

    monkeypatch.setattr(move_ledger, "own_provider_team_id", lambda key, info: None)
    monkeypatch.setattr(league_overview, "read_activity",
                        lambda q: {"since": "x", "through": None, "total": 0, "by_team": [], "moves": []})
    body = json.loads(_run("get_league_activity", {"team_id": 21}).content)
    assert "No rosters are stored" in body["note"]


def test_league_activity_is_espn_only_and_owned(owned, monkeypatch):
    yahoo = SimpleNamespace(**{**vars(TEAM), "league_info_json": json.dumps(
        {"provider": "yahoo", "league_id": 5, "team_name": "Y", "year": 2027})})
    monkeypatch.setattr(tools, "ensure_team_owned", lambda team_id, user_id: yahoo)
    out = _run("get_league_activity", {"team_id": 21})
    assert out.is_error and "ESPN leagues only" in out.content
    monkeypatch.setattr(tools, "ensure_team_owned", lambda team_id, user_id: None)
    out = _run("get_league_activity", {"team_id": 21})
    assert out.is_error and "not one of the user's teams" in out.content


# ---------------------------- get_draft_board ---------------------------- #


def _row(pid, name, rank, **kw):
    base = dict(player_id=pid, name=name, team="DEN", primary_position="C", positions=["C", "UT", "BE"],
                value=50.0 + rank, value_source="projection", board_rank=rank, cv_rank=rank, market_rank=rank + 1,
                adp=rank + 0.5, room_score=900.0 - rank, room_rank=rank, availability="likely")
    base.update(kw)
    return DraftBoardRow(**base)


def _board():
    rows = [
        _row(1, "Big One", 1, availability="gone"),
        _row(2, "Guard One", 2, primary_position="PG", positions=["PG", "SG", "G", "UT"]),
        _row(3, "Wing One", 3, primary_position="SF", positions=["SF", "F", "UT"], injury_status="DAY_TO_DAY"),
    ]
    recs = [DraftRecommendation(
        player_id=2, name="Guard One", primary_position="PG", value=52.04, season_value=3800.44, vorp=410.26,
        score=395.0, market_rank=3, cv_rank=2, reason="Most value over replacement on the board",
        components=[RecommendationComponent(key="vorp", label="Over replacement", value=410.26, in_score=True),
                    RecommendationComponent(key="congestion", label="Congestion", value=-15.26, in_score=True,
                                            detail="Benches a centre two nights a week")],
    )]
    meta = DraftBoardMeta(season="2026-27", format="categories", value_kind="cat_value", pool_size=300,
                          available=297, projection_count=280, baseline_count=17, rank_source="cv",
                          rank_basis="espn", punts=["ft_pct"],
                          category_need=[CategoryNeedResp(key="blk", label="BLK", mine=0.5, pace=1.2, need=0.734,
                                                          weight=1.3, punted=False, my_rank=3, seats=4)])
    return DraftBoardResp(status=ApiStatus.SUCCESS, message="ok", data=rows, recommendations=recs, meta=meta,
                          roster=[DraftRosterEntry(player_id=9, name="Drafted", team="BOS", primary_position="SG",
                                                   value=48.123)])


@pytest.fixture
def room(monkeypatch):
    import api.deps
    from services.draft_board_service import DraftBoardService
    from services.draft_service import DraftService

    asked = {}
    ctx = SimpleNamespace(session_id=5, user_id=42, team_id=21, league_id=7, league=None, kind="mock",
                          status="drafting", draft_type="snake", pick_order=(1, 2, 3, 4), my_slot=2, rounds=13,
                          punts=("ft_pct",), scoring_format="categories", espn_league_id=None, league_size=4)

    async def get_board(scoring, picked_ids=(), my_ids=(), session=None):
        asked.update(scoring=scoring.format, rank_source=session.rank_source, session_id=session.session_id)
        return _board()

    async def get_session(session_id):
        return SimpleNamespace(data=SimpleNamespace(name="Mock 1", status="drafting", pick_count=6,
                                                    my_next_pick=7, picks_until_my_turn=0))

    monkeypatch.setattr(api.deps, "_owned_session", lambda room_id, user_id: ctx if room_id == 5 else None)
    monkeypatch.setattr(tools, "run_db", _run_db)
    monkeypatch.setattr(DraftBoardService, "get_board", get_board)
    monkeypatch.setattr(DraftService, "get_session", get_session)
    return asked


def test_get_draft_board_shows_the_recommendations_with_every_term(room):
    out = _run("get_draft_board", {"room_id": 5, "rank_source": "espn"})
    assert not out.is_error, out.content
    body = json.loads(out.content)
    assert room == {"scoring": "categories", "rank_source": "espn", "session_id": 5}
    assert (body["my_next_pick"], body["picks_until_my_turn"], body["picks_made"]) == (7, 0, 6)
    assert body["format"] == "categories" and body["punts"] == ["ft_pct"] and body["board_order"] == "espn"
    rec = body["recommendations"][0]
    assert rec["player_id"] == 2 and rec["score"] == 395.0 and rec["vorp"] == 410.3 and rec["value"] == 52.04
    assert rec["availability"] == "likely" and rec["adp"] == 2.5 and rec["team"] == "DEN"
    assert rec["components"][1] == {"key": "congestion", "label": "Congestion", "value": -15.3, "in_score": True,
                                    "detail": "Benches a centre two nights a week"}
    assert [r["name"] for r in body["best_available"]] == ["Big One", "Guard One", "Wing One"]
    assert body["best_available"][1]["positions"] == "PG/SG"
    assert body["roster"] == [{"player_id": 9, "name": "Drafted", "team": "BOS", "position": "SG", "value": 48.12}]
    assert body["category_need"] == [{"category": "blk", "label": "BLK", "need": 0.73, "punted": False,
                                      "my_rank": 3, "of": 4}]


def test_best_available_can_be_one_position(room):
    body = json.loads(_run("get_draft_board", {"room_id": 5, "position": "G", "available": 5}).content)
    assert [r["name"] for r in body["best_available"]] == ["Guard One"] and body["position"] == "G"
    body = json.loads(_run("get_draft_board", {"room_id": 5, "position": "F"}).content)
    assert [r["name"] for r in body["best_available"]] == ["Wing One"]


def test_get_draft_board_refuses_a_room_that_is_not_the_user_s(room):
    out = _run("get_draft_board", {"room_id": 6})
    assert out.is_error and "not one of the user's rooms" in out.content
