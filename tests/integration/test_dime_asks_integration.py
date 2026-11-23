"""
Integration: the user's asks (services/ai/asks.py; migration 0055) against the real schema.

Dime saves an ask from the user's own turn (never a check-in's), for a later time within two weeks,
ten waiting a team at most; the user or Dime cancels one that hasn't run. A due ask runs as a check-in
of kind ask: one at a time per team (a busy team's ask waits), its spend on the check-ins line (a
refused budget skips it), its conversation titled with the ask, its brief the user's words and its
proposals live. One past its hour of grace is missed; one cancelled as it starts leaves no check-in.
The timeline shows the day's waiting asks and the ask beside the check-in that ran it, and a chat's
ask block reads as the ask stands now.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.errors import ConflictError, NotFoundError
from core.nba_calendar import EASTERN
from core.settings import settings
from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.ai_conversations import AiConversation, AiSpend
from db.models.autopilot import AiAsk, AiCheckin
from db.models.teams import Team
from db.models.users import User
from services.ai import agent, asks, checkins, conversations, spend, timeline, tools

pytestmark = [pytest.mark.integration]


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _user(n):
    return User.create(email=f"ask{n}@courtvision.dev", clerk_user_id=f"user_ask_{n}", created_at=_now())


def _team(user, name="Mine"):
    info = {"provider": "espn", "league_id": 1234, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"1234{name}", league_info=json.dumps(info))


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "autopilot_monthly_budget_usd", 100.0)
    monkeypatch.setattr(settings, "ai_agent_turn_max_usd", 1.0)
    return monkeypatch


@pytest.fixture
def mine(on):
    me = _user(1)
    team = _team(me)
    on.setattr(settings, "ai_agent_user_ids", [me.user_id])
    chat = run(conversations.create(me.user_id, team.team_id, title="Tonight"))
    return me, team, chat


def _ctx(user, conversation, checkin_id=None):
    return tools.ToolContext(user_id=user.user_id, conversation_id=conversation.id, checkin_id=checkin_id)


def _schedule(user, team, conversation, at, what="Check whether Embiid plays; if not, start Herb Jones", **kw):
    out = run(tools.run_tool("schedule_ask", {"team_id": team.team_id, "at": at, "what": what},
                             ctx=_ctx(user, conversation, **kw), allowed=agent.ALLOWED))
    return out, (json.loads(out.content) if not out.is_error else None)


def _ask(user, team, conversation, due_at, what="Swap out Embiid if he sits", state="pending"):
    return AiAsk.create(user_id=user.user_id, team_id=team.team_id, conversation_id=conversation.id, what=what,
                        due_at=due_at, state=state, created_at=due_at - timedelta(hours=3))


def _fake_turn(monkeypatch, act=None):
    """The agent's turn, stubbed: `act(turn)` does what a model would; the reservation is settled and the
    conversation released, as a written turn would."""
    calls = []

    async def run_turn(turn, emit, *, header_line):
        calls.append(turn)
        if act is not None:
            await act(turn)
        await spend.settle(turn.reservation_id, 1000)
        await conversations.release(turn.conversation_id)
        return agent.TurnResult(turn_id=None, outcome="ok", cost=1000)

    async def header(user_id, pinned, now=None, *, conversation_id=None):
        return f"[header {pinned}]"

    monkeypatch.setattr(agent, "run_turn", run_turn)
    monkeypatch.setattr(agent, "header", header)
    return calls


async def _drain():
    while checkins._RUNNING:
        await asyncio.gather(*list(checkins._RUNNING))


class TestSaving:
    def test_dime_saves_one_from_the_user_s_turn_and_the_chat_gets_its_block(self, mine):
        me, team, chat = mine
        at = (_now() + timedelta(hours=2)).astimezone(EASTERN).replace(tzinfo=None, second=0)
        out, data = _schedule(me, team, chat, at.isoformat())
        assert not out.is_error, out.content
        row = AiAsk.get_by_id(data["ask_id"])
        # A wall time without an offset is Eastern; stored to the minute, in UTC
        assert row.due_at == EASTERN.localize(at).astimezone(timezone.utc)
        assert (row.state, row.team_id_id, row.conversation_id_id) == ("pending", team.team_id, chat.id)
        block = data[asks.ASK_KEY]
        assert block["state"] == "pending" and block["what"].startswith("Check whether Embiid")
        assert datetime.fromisoformat(block["due_at"]) == row.due_at
        assert data["at_et"] == asks.et_words(row.due_at) and data["saved"] is True

    def test_refused_in_a_check_in_in_the_past_too_far_ahead_or_past_ten_waiting(self, mine):
        me, team, chat = mine
        later = (_now() + timedelta(hours=1)).isoformat()
        out, _ = _schedule(me, team, chat, later, checkin_id=5)
        assert out.is_error and "this is a check-in" in out.content
        out, _ = _schedule(me, team, chat, (_now() - timedelta(minutes=5)).isoformat())
        assert out.is_error and "has passed" in out.content
        out, _ = _schedule(me, team, chat, (_now() + timedelta(days=15)).isoformat())
        assert out.is_error and "two weeks" in out.content
        for n in range(asks.PER_TEAM):
            _ask(me, team, chat, _now() + timedelta(hours=n + 1))
        out, _ = _schedule(me, team, chat, later)
        assert out.is_error and "10 asks waiting" in out.content
        assert AiAsk.select().count() == asks.PER_TEAM

    def test_two_turns_at_once_cannot_both_take_the_last_place(self, mine):
        me, team, chat = mine
        other = run(conversations.create(me.user_id, team.team_id, title="Also tonight"))
        for n in range(asks.PER_TEAM - 1):
            _ask(me, team, chat, _now() + timedelta(hours=n + 1))
        at = _now() + timedelta(hours=20)

        async def both():
            return await asyncio.gather(asks._admit(me.user_id, team.team_id, chat.id, "One", at, _now()),
                                        asks._admit(me.user_id, team.team_id, other.id, "Two", at, _now()))

        saved = [r for r in run(both()) if r is not None]
        assert len(saved) == 1
        assert AiAsk.select().where(AiAsk.state == "pending").count() == asks.PER_TEAM

    def test_only_for_the_user_s_own_team(self, mine):
        me, _, chat = mine
        them = _user(2)
        theirs = _team(them, "Theirs")
        out, _ = _schedule(me, theirs, chat, (_now() + timedelta(hours=1)).isoformat())
        assert out.is_error and AiAsk.select().count() == 0

    def test_off_on_the_server_it_says_so(self, mine, on):
        me, team, chat = mine
        on.setattr(settings, "autopilot_enabled", False)
        out, _ = _schedule(me, team, chat, (_now() + timedelta(hours=1)).isoformat())
        assert out.is_error and "switched off" in out.content


class TestCancelling:
    def test_the_user_cancels_a_waiting_one_once_and_only_their_own(self, mine):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() + timedelta(hours=1))
        them = _user(2)
        with pytest.raises(NotFoundError):
            run(asks.cancel(them.user_id, ask.id))
        view = run(asks.cancel(me.user_id, ask.id))
        assert (view["state"], view["reason"]) == ("cancelled", "user")
        with pytest.raises(ConflictError):
            run(asks.cancel(me.user_id, ask.id))

    def test_dime_cancels_one_when_asked_but_not_from_a_check_in(self, mine):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() + timedelta(hours=1))
        args = {"team_id": team.team_id, "ask_id": ask.id}
        out = run(tools.run_tool("cancel_ask", args, ctx=_ctx(me, chat, checkin_id=9), allowed=agent.CHECKIN_ALLOWED))
        assert out.is_error and AiAsk.get_by_id(ask.id).state == "pending"
        out = run(tools.run_tool("cancel_ask", args, ctx=_ctx(me, chat), allowed=agent.ALLOWED))
        data = json.loads(out.content)
        assert data["cancelled"] is True and data[asks.ASK_KEY]["state"] == "cancelled"
        # A draft note's tools have no asks at all
        out = run(tools.run_tool("cancel_ask", args, ctx=_ctx(me, chat), allowed=agent.DRAFT_ALLOWED))
        assert out.is_error and out.content == "Unknown tool: cancel_ask"


class TestRunning:
    def test_a_due_ask_runs_as_a_check_in_with_the_user_s_words_and_live_chips(self, mine, monkeypatch):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() - timedelta(minutes=1), what="Start Herb Jones if Embiid sits")
        later = _ask(me, team, chat, _now() + timedelta(hours=1))
        seen = {}

        async def act(turn):
            row = AiCheckin.get_by_id(turn.checkin_id)
            seen["state"] = asks.view(AiAsk.get_by_id(ask.id), row)["state"]

        calls = _fake_turn(monkeypatch, act)

        async def go():
            out = await asks.run_due()
            await _drain()
            return out

        assert run(go()) == {"started": 1, "waiting": 0, "closed": 0}
        turn = calls[0]
        assert turn.live_chips is True and turn.tools is agent.CHECKIN_TOOLS
        assert turn.checkin_id is not None and turn.limits == agent.Limits.checkin()
        assert '"Start Herb Jones if Embiid sits"' in turn.message
        assert turn.message.startswith(f"[The user's ask for team_id {team.team_id}")
        assert "No automation is at AI on this team" in turn.message
        checkin = AiCheckin.get_by_id(turn.checkin_id)
        assert (checkin.kind, checkin.manual, checkin.state) == ("ask", True, "done")
        assert AiConversation.get_by_id(checkin.conversation_id_id).title.startswith("You asked: Start Herb Jones")
        row = AiAsk.get_by_id(ask.id)
        assert (row.state, row.checkin_id_id) == ("started", checkin.id)
        assert seen["state"] == "running" and asks.view(row, checkin)["state"] == "done"
        # One Dime turn on the check-ins line, not the desk's
        assert [s.line for s in AiSpend.select()] == ["autopilot"]
        assert AiAsk.get_by_id(later.id).state == "pending"

    def test_a_busy_team_s_ask_waits_for_the_check_in_and_then_runs(self, mine, monkeypatch):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() - timedelta(minutes=2))
        busy = run(checkins._claim(team.team_id, me.user_id, "morning", date.today(), _now(), manual=False,
                                   now=_now()))
        calls = _fake_turn(monkeypatch)

        async def go():
            out = await asks.run_due()
            await _drain()
            return out

        assert run(go()) == {"started": 0, "waiting": 1, "closed": 0}
        assert AiAsk.get_by_id(ask.id).state == "pending" and calls == []
        # The ask's lost claim left no row and handed back its reservation
        assert AiCheckin.select().where(AiCheckin.kind == "ask").count() == 0
        assert AiSpend.select().where(AiSpend.cost_micros > 0).count() == 0
        run(checkins._settle(busy, "done", None, None, 0, _now()))
        assert run(go())["started"] == 1 and len(calls) == 1

    def test_a_check_in_that_died_with_its_process_is_failed_and_the_ask_runs(self, mine, monkeypatch):
        # A restart mid-turn leaves a check-in running; with check-ins off nothing else sweeps it
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() - timedelta(minutes=1))
        long_ago = _now() - timedelta(seconds=settings.autopilot_checkin_timeout_seconds) - timedelta(minutes=11)
        dead = run(checkins._claim(team.team_id, me.user_id, "morning", date.today(), long_ago, manual=False,
                                   now=long_ago))
        calls = _fake_turn(monkeypatch)

        async def go():
            out = await asks.run_due()
            await _drain()
            return out

        assert run(go()) == {"started": 1, "waiting": 0, "closed": 0}
        assert (AiCheckin.get_by_id(dead).state, AiCheckin.get_by_id(dead).reason) == ("failed", "stale")
        assert AiAsk.get_by_id(ask.id).state == "started" and len(calls) == 1

    def test_two_due_on_one_team_run_one_a_look(self, mine, monkeypatch):
        me, team, chat = mine
        _ask(me, team, chat, _now() - timedelta(minutes=3))
        _ask(me, team, chat, _now() - timedelta(minutes=2))
        _fake_turn(monkeypatch)

        async def go():
            out = await asks.run_due()
            await _drain()
            return out

        assert run(go()) == {"started": 1, "waiting": 1, "closed": 0}
        assert run(go()) == {"started": 1, "waiting": 0, "closed": 0}

    def test_late_not_allowed_or_over_budget_closes_it_instead(self, mine, on, monkeypatch):
        me, team, chat = mine
        late = _ask(me, team, chat, _now() - asks.GRACE - timedelta(minutes=1))
        calls = _fake_turn(monkeypatch)
        run(asks.run_due())
        assert (AiAsk.get_by_id(late.id).state, AiAsk.get_by_id(late.id).reason) == ("missed", "late")

        budget = _ask(me, team, chat, _now() - timedelta(minutes=1))
        on.setattr(settings, "autopilot_monthly_budget_usd", 0.5)
        run(asks.run_due())
        assert (AiAsk.get_by_id(budget.id).state, AiAsk.get_by_id(budget.id).reason) == ("skipped", "budget")

        gone = _ask(me, team, chat, _now() - timedelta(minutes=1))
        on.setattr(settings, "ai_agent_user_ids", [])
        run(asks.run_due())
        assert (AiAsk.get_by_id(gone.id).state, AiAsk.get_by_id(gone.id).reason) == ("skipped", "not_allowed")
        assert calls == [] and AiCheckin.select().count() == 0

    def test_one_cancelled_as_it_starts_leaves_no_check_in(self, mine, monkeypatch):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() - timedelta(minutes=1))
        calls = _fake_turn(monkeypatch)
        stale = AiAsk.get_by_id(ask.id)          # read before the cancel, as a look would have
        run(asks.cancel(me.user_id, ask.id))
        assert run(checkins.start_ask(stale, _now())) is None
        assert calls == [] and AiCheckin.select().count() == 0
        assert AiAsk.get_by_id(ask.id).state == "cancelled"

    def test_off_on_the_server_nothing_is_read(self, mine, on):
        me, team, chat = mine
        _ask(me, team, chat, _now() - timedelta(minutes=1))
        on.setattr(settings, "autopilot_enabled", False)
        assert run(asks.run_due()) == {"started": 0, "waiting": 0, "closed": 0}


class TestViews:
    def test_the_timeline_shows_the_day_s_waiting_asks_and_the_ask_beside_its_check_in(self, mine, monkeypatch):
        me, team, chat = mine
        now = _now()
        today = checkins.fantasy_day(now)
        start, end = timeline.bounds(today)
        ran = _ask(me, team, chat, now - timedelta(minutes=1), what="Ran already")
        _fake_turn(monkeypatch)

        async def go():
            await asks.run_due()
            await _drain()

        run(go())
        waiting = _ask(me, team, chat, min(now + timedelta(minutes=30), end - timedelta(minutes=1)), what="Later")
        tomorrow = _ask(me, team, chat, end + timedelta(hours=1), what="Tomorrow")
        day = run(timeline.day(me.user_id, team.team_id, today, now=now))
        assert [a["ask_id"] for a in day["asks"]] == [waiting.id]
        assert day["asks"][0]["state"] == "pending"
        mine_ran = [c for c in day["checkins"] if c["kind"] == "ask"]
        assert len(mine_ran) == 1 and mine_ran[0]["ask"]["ask_id"] == ran.id and mine_ran[0]["ask"]["state"] == "done"
        assert mine_ran[0]["title"].startswith("Your ask")
        listed = run(asks.team_asks(me.user_id, team.team_id))
        assert [a["ask_id"] for a in listed["waiting"]] == [waiting.id, tomorrow.id]
        assert [a["ask_id"] for a in listed["recent"]] == [ran.id]

    def test_a_chat_s_ask_block_reads_as_the_ask_stands_now(self, mine):
        me, team, chat = mine
        _, data = _schedule(me, team, chat, (_now() + timedelta(hours=1)).isoformat())
        part = {"kind": "block", "block": {"type": "ask", "id": "a1", **data[asks.ASK_KEY]}}
        run(asks.cancel(me.user_id, data["ask_id"]))
        history = run(asks.refresh_history([[part, {"kind": "text", "text": "Saved"}]], me.user_id))
        fresh, text = history[0]
        assert fresh["block"]["state"] == "cancelled" and fresh["block"]["id"] == "a1" and text["text"] == "Saved"
        # Someone else's history never reads this user's asks
        them = _user(2)
        other = run(asks.refresh_history([[part]], them.user_id))[0][0]
        assert other["block"]["state"] == "pending"

    def test_get_automations_lists_the_waiting_ones_for_dime(self, mine):
        me, team, chat = mine
        ask = _ask(me, team, chat, _now() + timedelta(hours=1))
        _ask(me, team, chat, _now() + timedelta(hours=2), state="cancelled")
        listed = run(asks.waiting_for_model(team.team_id, me.user_id))
        assert [a["ask_id"] for a in listed] == [ask.id] and listed[0]["at_et"] == asks.et_words(ask.due_at)


def test_0055_rollback_applies_and_the_forward_file_restores_it(integration_db):
    forward = (Path(MIGRATIONS_DIR) / "0055__dime_asks.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0055__dime_asks.rollback.sql").read_text()
    try:
        db.execute_sql(rollback)
        assert db.execute_sql("SELECT to_regclass('usr.ai_asks')").fetchone()[0] is None
    finally:
        db.execute_sql(forward)
    assert db.execute_sql("SELECT to_regclass('usr.ai_asks')").fetchone()[0] is not None
    check = db.execute_sql("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                           "WHERE conname = 'ai_checkins_kind_check'").fetchone()[0]
    assert "'ask'" in check and "'event'" in check
