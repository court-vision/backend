"""
Integration: Dime in the Draft desk against the real schema (migration 0046).

The claim that runs one note per room at a time and one note per pick (a failed one
leaves room for a retry), the plan that sets a room's punts (checked by the room's own
update) and saves to the team's notes only in a real draft, the pick Dime makes in the
user's mock seat (recorded `dime`, by the user's seat, at the pick on the clock, once),
the seat drafting a whole mock with the autopicker running the other seats, the recap's
read of it, a room Dime drafted in refusing to follow an ESPN draft, and the rollback.

The agent's turn is stubbed: it makes the tool calls a model would, through the real
tool registry and the real rows.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.errors import ConflictError
from core.settings import settings
from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.ai_conversations import AiConversation, AiSpend
from db.models.ai_conversations import AiMemory
from db.models.dime_draft import AiDraftNote, AiDraftRoom
from db.models.drafts import DraftPick
from db.models.leagues import League
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User
from schemas.draft import DraftInitSyncRequest, DraftSessionCreate
from services.ai import agent, conversations, draft, spend, tools
from services.draft_service import DraftService
from services.draft_sync_service import DraftSyncService
from services.scoring.pool import baseline_season
from api.deps import _owned_session
from utils.espn_draft_init import strip_init_prefix

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOMOPEN = strip_init_prefix((FIXTURES / "espn_draft_init_roomopen.b64").read_text())
ROSTER_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1}
POSITIONS = ["PG", "SG", "SF", "PF", "C"]
ELIGIBLE = {"PG": [0, 5, 11], "SG": [1, 5, 11], "SF": [2, 6, 11], "PF": [3, 6, 11], "C": [4, 11]}
MY_SLOT = 3
POOL_SIZE = 100


@pytest.fixture
def on(monkeypatch, integration_db):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "dime_draft_enabled", True)
    monkeypatch.setattr(settings, "ai_monthly_budget_usd", 100.0)
    monkeypatch.setattr(settings, "ai_agent_turn_max_usd", 1.0)


@pytest.fixture
def league(integration_db, replay):
    return League.create(
        provider="espn", provider_league_id=str(replay["league_id"]), season=2027, name="Mock League",
        scoring_type="points", categories=[],
        point_weights={"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "tov": -1.0},
        matchup_periods={}, roster_slots=ROSTER_SLOTS, position_limits={"C": 4},
        draft_settings={"type": replay["draft_type"].upper(), "pick_order": replay["pick_order"], "keeper_count": 0},
        raw_settings={}, settings_synced_at=datetime.utcnow(),
    )


@pytest.fixture
def team(league, on, monkeypatch):
    user = User.create(email="dimedraft@courtvision.dev", clerk_user_id="user_dime_draft", created_at=datetime.utcnow())
    monkeypatch.setattr(settings, "ai_agent_user_ids", [user.user_id])
    return Team.create(user_id=user.user_id, team_identifier="dimedraft", league_info="{}", league=league)


@pytest.fixture
def pool(integration_db):
    ids = []
    season = baseline_season()
    for index in range(POOL_SIZE):
        nba_id = 900_001 + index
        position = POSITIONS[index % len(POSITIONS)]
        Player.create(id=nba_id, espn_id=500_001 + index, name=f"Mock {index + 1:03d}",
                      name_normalized=f"mock {index + 1:03d}", position=position)
        per_game, gp = 40.0 - index * 0.3, 70
        PlayerSeasonStats.create(
            player=nba_id, team=None, as_of_date=date(2025, 4, 10), season=season, gp=gp,
            fpts=int(per_game * gp), pts=int(per_game * gp), reb=gp * 5, ast=gp * 3, stl=gp, blk=gp, tov=gp * 2,
            min=gp * 30, fgm=gp * 8, fga=gp * 16, fg3m=gp * 2, fg3a=gp * 5, ftm=gp * 4, fta=gp * 5,
        )
        DraftMarket.create(
            player=nba_id, season=settings.nba_season, source="espn", as_of_date=date(2026, 9, 1),
            overall_rank=index + 1, adp=float(index + 1), auction_value=None,
            default_position_id=POSITIONS.index(position) + 1, eligible_slot_ids=ELIGIBLE[position],
            injury_status=None,
        )
        ids.append(nba_id)
    return ids


async def _room(team, kind="mock", **overrides):
    body = dict(team_id=team.team_id, kind=kind, my_slot=MY_SLOT)
    body.update(overrides)
    session = (await DraftService.create_session(team.user_id_id, DraftSessionCreate(**body))).data
    return await _ctx(session.id, team.user_id_id)


async def _ctx(session_id, user_id):
    from db.base import run_db
    return await run_db("test.owned_session", _owned_session, session_id, user_id)


def _board_in(message):
    return json.loads(message.split("cite these numbers):\n", 1)[1].split("\n\n", 1)[0])


def _fake_turn(monkeypatch, act):
    """The agent's turn, stubbed: `act(turn)` makes the tool calls a model would and returns its
    answer; the reservation is settled and the conversation released, as a written turn would."""
    calls = []

    async def run_turn(turn, emit, *, header_line):
        calls.append(turn)
        text = await act(turn)
        if text:
            turn.add_text(text)
        await spend.settle(turn.reservation_id, 1000)
        await conversations.release(turn.conversation_id)
        return agent.TurnResult(turn_id=None, outcome="ok", cost=1000)

    monkeypatch.setattr(agent, "run_turn", run_turn)
    return calls


async def _tool(turn, name, args):
    ctx = tools.ToolContext(user_id=turn.user_id, conversation_id=turn.conversation_id,
                            draft_note_id=turn.draft_note_id)
    return await tools.run_tool(name, args, ctx=ctx, allowed=turn.allowed)


async def _settled(started):
    if started.task is not None:
        await started.task
    return AiDraftNote.get_by_id(started.note_id)


# ------------------------------- the claim ------------------------------- #


async def test_one_note_runs_per_room_and_one_per_pick(team, pool):
    room = await _room(team)
    first, held = await draft._claim(room.session_id, team.user_id_id, draft.PICK, 3, False, {})
    assert first is not None and held is None
    # Another note while it runs: refused at the insert
    assert await draft._claim(room.session_id, team.user_id_id, draft.PLAN, None, False, {}) == (None, None)
    await draft._settle(first, draft.DONE, None, "Take him", None, 0)
    # The same pick again: the note already written comes back
    assert await draft._claim(room.session_id, team.user_id_id, draft.PICK, 3, False, {}) == (None, first)
    # The seat's pick at the same number is its own note
    seat, _ = await draft._claim(room.session_id, team.user_id_id, draft.PICK, 3, True, {})
    assert seat is not None
    await draft._settle(seat, draft.FAILED, "no_pick", None, None, 0)
    # A failed one leaves room for a retry
    retry, _ = await draft._claim(room.session_id, team.user_id_id, draft.PICK, 3, True, {})
    assert retry is not None


async def test_a_note_whose_process_died_is_swept(team, pool):
    room = await _room(team)
    note_id, _ = await draft._claim(room.session_id, team.user_id_id, draft.PLAN, None, False, {})
    AiDraftNote.update(created_at=datetime.now(timezone.utc) - timedelta(hours=1)).where(
        AiDraftNote.id == note_id).execute()
    view = await draft.room_view(team.user_id_id, room)
    assert view["running"] is None and view["notes"][0]["state"] == "failed" and view["notes"][0]["reason"] == "stale"


# ------------------------------- a pick note ------------------------------- #


async def test_a_pick_note_reads_the_board_first_and_is_written_once(team, pool, monkeypatch):
    room = await _room(team)

    async def act(turn):
        board = _board_in(turn.message)
        return f"Take {board['recommendations'][0]['name']}."

    calls = _fake_turn(monkeypatch, act)
    note = await _settled(await draft.start(team.user_id_id, room, draft.PICK))
    assert note.state == "done" and note.for_pick == MY_SLOT and note.text == "Take Mock 001."
    assert note.facts["top"][0]["name"] == "Mock 001"
    # Seat 3, two picks away: who won't last is about pick 3 itself
    assert note.facts["on_clock"] is False and note.facts["picks_until"] == 2 and note.facts["gone_before"] == 3
    conv = AiConversation.get_by_id(note.conversation_id_id)
    assert conv.kind == "draft" and conv.title == "Dime · Mock draft · pick 3"
    assert [t["name"] for t in calls[0].tools][-2:] == ["set_draft_plan", "draft_pick"]
    # Asked again for the same pick: the one written comes back, with no new turn
    again = await draft.start(team.user_id_id, room, draft.PICK)
    assert again.note_id == note.id and again.task is None and len(calls) == 1
    # The note's turn spent on the AI desk's line
    assert AiSpend.select().where(AiSpend.user_id == team.user_id_id, AiSpend.line == "desk").count() == 1
    # Not in the AI desk's chats
    assert await conversations.list_for(team.user_id_id) == []


# ------------------------------- the plan ------------------------------- #


async def test_the_plan_sets_a_category_room_s_punts_and_a_mock_s_plan_stays_in_the_room(team, pool, monkeypatch):
    room = await _room(team, team_id=None, pick_order=[1, 2, 3, 4], rounds=13, scoring_format="categories")

    async def act(turn):
        out = await _tool(turn, "set_draft_plan", {"room_id": room.session_id, "plan": "Concede FT% and turnovers",
                                                   "punts": ["ft_pct", "tov"]})
        assert not out.is_error, out.content
        assert json.loads(out.content)["punts"] == ["ft_pct", "tov"]
        bad = await _tool(turn, "set_draft_plan", {"room_id": room.session_id, "plan": "x", "punts": ["dunks"]})
        assert bad.is_error and "Not a category in this league" in bad.content
        return "Punt FT% and TO."

    _fake_turn(monkeypatch, act)
    note = await _settled(await draft.start(team.user_id_id, room, draft.PLAN))
    assert note.state == "done" and note.plan == "Concede FT% and turnovers"
    assert note.punts == ["ft_pct", "tov"] and note.punts_before == []
    assert (await DraftService.get_session(room.session_id)).data.punts == ["ft_pct", "tov"]
    assert AiMemory.select().count() == 0
    # A pick note reads the plan in its brief
    seen = []

    async def pick(turn):
        seen.append(turn.message)
        return "Take him."

    _fake_turn(monkeypatch, pick)
    await _settled(await draft.start(team.user_id_id, await _ctx(room.session_id, team.user_id_id), draft.PICK))
    assert 'The room\'s plan: "Concede FT% and turnovers" (punts: ft_pct, tov).' in seen[0]


async def test_a_real_draft_s_plan_is_saved_to_the_team_s_notes(team, pool, monkeypatch):
    room = await _room(team, kind="manual")

    async def act(turn):
        out = await _tool(turn, "set_draft_plan", {"room_id": room.session_id, "plan": "Guards early, bigs late"})
        assert not out.is_error, out.content
        return "Guards early."

    _fake_turn(monkeypatch, act)
    note = await _settled(await draft.start(team.user_id_id, room, draft.PLAN))
    assert note.punts == []
    saved = list(AiMemory.select())
    assert [(m.kind, m.team_id_id, m.text, m.author) for m in saved] == [
        ("plan", team.team_id, "Draft plan: Guards early, bigs late", "dime")]


async def test_a_finished_draft_takes_no_plan_and_an_empty_one_no_recap(team, pool, monkeypatch):
    room = await _room(team)
    with pytest.raises(ConflictError) as err:
        await draft.start(team.user_id_id, room, draft.RECAP)
    assert err.value.error_code == "AI_DRAFT_NO_PICKS"
    from schemas.draft import DraftSessionUpdate
    await DraftService.update_session(room.session_id, DraftSessionUpdate(status="completed"))
    with pytest.raises(ConflictError) as err:
        await draft.start(team.user_id_id, await _ctx(room.session_id, team.user_id_id), draft.PLAN)
    assert err.value.error_code == "AI_DRAFT_FINISHED"


# ------------------------------- the seat ------------------------------- #


async def test_dime_s_pick_is_the_user_s_at_the_pick_on_the_clock_and_made_once(team, pool, monkeypatch):
    room = await _room(team)
    outcomes = {}

    async def act(turn):
        board = _board_in(turn.message)
        top = board["recommendations"][0]["player_id"]
        outcomes["taken"] = await _tool(turn, "draft_pick", {"room_id": room.session_id, "player_id": top,
                                                             "reason": "Best on the board"})
        outcomes["twice"] = await _tool(turn, "draft_pick", {"room_id": room.session_id, "player_id": top + 1,
                                                             "reason": "Again"})
        return "Took the best on the board."

    _fake_turn(monkeypatch, act)
    # Pick 3 is the user's; the other seats go first
    from services.draft_mock_service import DraftMockService
    from schemas.draft import MockAdvanceRequest
    await DraftMockService.advance(room.session_id, MockAdvanceRequest(until="my_turn"))
    note = await _settled(await draft.start(team.user_id_id, room, draft.PICK, seat=True))
    assert not outcomes["taken"].is_error, outcomes["taken"].content
    assert outcomes["twice"].is_error and "You already took" in outcomes["twice"].content
    assert note.state == "done" and note.seat and note.took["overall_pick"] == 3
    pick = DraftPick.get(DraftPick.session == room.session_id, DraftPick.overall_pick == 3)
    assert pick.source == "dime" and pick.by_me is True and pick.player_id == note.took["player_id"]
    # Off the clock now: a seat pick is refused before any turn
    with pytest.raises(ConflictError) as err:
        await draft.start(team.user_id_id, room, draft.PICK, seat=True)
    assert err.value.error_code == "AI_DRAFT_NOT_YOUR_TURN"
    # A room with Dime's picks in it is a simulated room: it can't start following an ESPN draft
    with pytest.raises(ConflictError) as err:
        await DraftSyncService.sync_init(room.session_id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert err.value.error_code == "DRAFT_ROOM_IS_SIMULATED"


async def test_a_turn_that_makes_no_pick_fails_its_note(team, pool, monkeypatch):
    room = await _room(team)
    from services.draft_mock_service import DraftMockService
    from schemas.draft import MockAdvanceRequest
    await DraftMockService.advance(room.session_id, MockAdvanceRequest(until="my_turn"))

    async def act(turn):
        drafted = DraftPick.select().where(DraftPick.session == room.session_id).first().player_id
        out = await _tool(turn, "draft_pick", {"room_id": room.session_id, "player_id": drafted, "reason": "x"})
        assert out.is_error and "isn't on the board" in out.content
        return "I couldn't pick."

    _fake_turn(monkeypatch, act)
    note = await _settled(await draft.start(team.user_id_id, room, draft.PICK, seat=True))
    assert note.state == "failed" and note.reason == "no_pick" and note.took is None


async def test_the_seat_drafts_a_whole_mock_and_the_recap_says_dime_made_the_picks(team, pool, monkeypatch, replay):
    room = await _room(team)

    async def act(turn):
        board = _board_in(turn.message)
        top = board["recommendations"][0]["player_id"]
        out = await _tool(turn, "draft_pick", {"room_id": room.session_id, "player_id": top, "reason": "Top call"})
        assert not out.is_error, out.content
        return "Took the top call."

    _fake_turn(monkeypatch, act)
    view = await draft.set_seat(team.user_id_id, room, True)
    assert view["seat"]["state"] == "drafting" and view["can_seat"] is True
    # Asked again while it drafts: no second loop
    await draft.set_seat(team.user_id_id, room, True)
    loops = [t for t in list(draft._RUNNING) if t.get_coro().__name__ == "seat_loop"]
    assert len(loops) == 1
    assert await loops[0] == "done"

    picks = list(DraftPick.select().where(DraftPick.session == room.session_id).order_by(DraftPick.overall_pick))
    assert len(picks) == len(replay["picks"]) == 52
    mine = [p for p in picks if p.by_me]
    assert len(mine) == 13 and all(p.source == "dime" for p in mine)
    assert all(p.source == "mock" for p in picks if not p.by_me)
    seat = AiDraftRoom.get_by_id(room.session_id)
    assert seat.seat == "off" and seat.seat_reason == "done"
    notes = list(AiDraftNote.select().where(AiDraftNote.session_id == room.session_id))
    assert len(notes) == 13 and all(n.seat and n.state == "done" for n in notes)
    assert sorted(n.for_pick for n in notes) == [p.overall_pick for p in mine]

    done = await _ctx(room.session_id, team.user_id_id)
    assert done.status == "completed"
    recap = await draft.get_draft_recap(team.user_id_id, draft.DraftRecapInput(room_id=room.session_id))
    assert len(recap["my_picks"]) == 13 and all(p["by_dime"] for p in recap["my_picks"])
    assert recap["complete"] is True and len(recap["seats"]) == 4
    assert sum(1 for s in recap["seats"] if s["mine"]) == 1
    # A finished room can't seat Dime again
    with pytest.raises(ConflictError) as err:
        await draft.set_seat(team.user_id_id, done, True)
    assert err.value.error_code == "AI_DRAFT_NO_SEAT"


async def test_taking_the_seat_back_stops_after_the_pick_being_made(team, pool, monkeypatch):
    room = await _room(team)

    async def act(turn):
        # The user takes the seat back while Dime is making this pick
        await draft.set_seat(team.user_id_id, room, False)
        assert AiDraftRoom.get_by_id(room.session_id).seat == "stopping"
        top = _board_in(turn.message)["recommendations"][0]["player_id"]
        out = await _tool(turn, "draft_pick", {"room_id": room.session_id, "player_id": top, "reason": "Top"})
        assert not out.is_error, out.content
        return "Took him."

    _fake_turn(monkeypatch, act)
    await draft.set_seat(team.user_id_id, room, True)
    loop = next(t for t in list(draft._RUNNING) if t.get_coro().__name__ == "seat_loop")
    assert await loop == "stopped"
    assert DraftPick.select().where(DraftPick.session == room.session_id, DraftPick.source == "dime").count() == 1
    row = AiDraftRoom.get_by_id(room.session_id)
    assert row.seat == "off" and row.seat_reason == "stopped"
    view = await draft.room_view(team.user_id_id, await _ctx(room.session_id, team.user_id_id))
    assert view["seat"] == {"state": "off", "reason": "stopped", "live": False}


async def test_a_live_room_never_seats_dime(team, pool):
    room = await _room(team, kind="live")
    with pytest.raises(ConflictError) as err:
        await draft.set_seat(team.user_id_id, room, True)
    assert err.value.error_code == "AI_DRAFT_NO_SEAT" and "only in a mock room" in err.value.message


async def test_the_notes_switch_is_kept_on_the_room(team, pool):
    room = await _room(team)
    view = await draft.set_notes(team.user_id_id, room, True)
    assert view["notes_on"] is True and view["available"] is True and view["lead"] == draft.NOTE_LEAD
    assert (await draft.set_notes(team.user_id_id, room, False))["notes_on"] is False


async def test_deleting_the_room_deletes_dime_s_rows(team, pool):
    room = await _room(team)
    await draft.set_notes(team.user_id_id, room, True)
    await draft._claim(room.session_id, team.user_id_id, draft.PLAN, None, False, {})
    await DraftService.delete_session(room.session_id)
    assert AiDraftRoom.select().count() == 0 and AiDraftNote.select().count() == 0


# ------------------------------- the migration ------------------------------- #


def test_0046_rollback_applies_and_the_forward_file_restores_it(integration_db):
    forward = (Path(MIGRATIONS_DIR) / "0046__dime_draft.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0046__dime_draft.rollback.sql").read_text()
    try:
        db.execute_sql(rollback)
        exists = db.execute_sql("SELECT to_regclass('usr.ai_draft_notes')").fetchone()[0]
        assert exists is None
    finally:
        db.execute_sql(forward)
    assert db.execute_sql("SELECT to_regclass('usr.ai_draft_notes')").fetchone()[0] is not None
