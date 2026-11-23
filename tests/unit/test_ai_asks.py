"""
The user's asks (services/ai/asks.py; the Dime desk redesign, step 2): what Dime saves (an Eastern
wall time, the user's words), what the desk shows (the ask's own state, then its check-in's), the
brief its check-in opens with, and the start path that takes the ask and runs it with live chips.
The claims and the clock run against the real schema in the integration tests.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.nba_calendar import EASTERN
from core.settings import settings
from services.ai import agent, asks, checkins, conversations, spend, tools

pytestmark = [pytest.mark.unit]


def run(coro):
    return asyncio.run(coro)


class TestInput:
    def test_a_wall_time_is_eastern_and_an_offset_is_kept(self):
        naive = asks.ScheduleAskInput(team_id=1, at="2026-11-22T18:30", what="check him")
        assert naive.at.astimezone(timezone.utc) == datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
        offset = asks.ScheduleAskInput(team_id=1, at="2026-11-22T18:30:00-05:00", what="check him")
        assert offset.at.astimezone(timezone.utc) == datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)

    def test_the_words_are_cleaned_and_bounded(self):
        assert asks.ScheduleAskInput(team_id=1, at="2026-11-22T18:30", what="  swap\n him \x00out ").what == "swap him out"
        with pytest.raises(ValidationError):
            asks.ScheduleAskInput(team_id=1, at="2026-11-22T18:30", what="x" * (asks.WHAT_MAX + 1))
        with pytest.raises(ValidationError):
            asks.ScheduleAskInput(team_id=1, at="2026-11-22T18:30", what="hi", extra=1)

    def test_eastern_words_read_as_the_user_does(self):
        assert asks.et_words(datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)) == "Sun Nov 22, 6:30 PM"
        assert asks.et_words(datetime(2026, 11, 23, 15, 5, tzinfo=timezone.utc)) == "Mon Nov 23, 10:05 AM"


def _row(**kw):
    base = dict(id=4, team_id_id=22, user_id_id=42, what="Start Herb Jones if Embiid sits", state="pending",
                reason=None, conversation_id_id=31, checkin_id_id=None,
                due_at=datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc),
                created_at=datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc))
    return SimpleNamespace(**{**base, **kw})


class TestViews:
    def test_the_desk_shows_the_ask_s_own_state_then_its_check_in_s(self):
        assert asks.state_of(_row(), None) == "pending"
        assert asks.state_of(_row(state="cancelled"), None) == "cancelled"
        started = _row(state="started", checkin_id_id=9)
        for state in ("running", "done", "failed"):
            assert asks.state_of(started, SimpleNamespace(state=state)) == state
        assert asks.state_of(started, None) == "done"

    def test_the_block_is_json_ready_and_opens_the_check_in_once_it_ran(self):
        view = asks.view(_row(state="started", checkin_id_id=9),
                         SimpleNamespace(id=9, state="done", conversation_id_id=77))
        block = asks.block_of(view)
        assert block["due_at"] == "2026-11-22T23:30:00+00:00"
        assert (block["state"], block["checkin_id"], block["conversation_id"]) == ("done", 9, 77)
        assert view["asked_in"] == 31


class TestBrief:
    def test_below_ai_every_change_is_a_chip_the_user_sends(self):
        text = checkins.ask_brief(22, "Start Herb Jones if Embiid sits", datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc),
                                  datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc), [])
        assert text.startswith("[The user's ask for team_id 22, due Sun Nov 22, 6:30 PM ET; they asked you for it "
                               "in a chat on Sun Nov 22, 1:00 PM ET.")
        assert "No automation is at AI on this team" in text and '"Start Herb Jones if Embiid sits"' in text
        assert checkins._SHADOW not in text

    def test_rows_at_ai_send_and_late_scratches_stay_with_event_check_ins(self):
        at = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
        text = checkins.ask_brief(22, "Swap him", at, at, ["lineup", "late_scratch"])
        assert "What you do yourself on this team: lineup (slot moves and pins). Those automations are at AI" in text
        assert "late scratches" not in text
        only_late = checkins.ask_brief(22, "Swap him", at, at, ["late_scratch"])
        assert "No automation is at AI on this team" in only_late


@pytest.fixture
def starting(monkeypatch):
    monkeypatch.setattr(settings, "autopilot_checkin_timeout_seconds", 60.0)
    state = SimpleNamespace(take=True, taken=[], dropped=[], cancelled=[], created=[], launched=[], events=[],
                            autopilot=None)

    async def reserve(user_id):
        return 55

    async def cancel(reservation_id):
        state.cancelled.append(reservation_id)

    async def claim(team_id, user_id, kind, nba_date, due_at, *, manual, now, state="running", reason=None):
        assert (kind, manual) == ("ask", True)
        return 7

    async def take(ask_id, checkin_id, now):
        state.taken.append((ask_id, checkin_id))
        return state.take

    async def drop(checkin_id):
        state.dropped.append(checkin_id)

    async def create(user_id, team_id, *, kind="chat", title=None):
        state.created.append((kind, title))
        return SimpleNamespace(id=31)

    async def conv_claim(conversation_id, user_id, hold):
        return True

    async def attach(checkin_id, conversation_id):
        pass

    async def header(user_id, pinned, now=None, *, conversation_id=None):
        return "[header]"

    async def autopilot_row(team_id):
        return state.autopilot

    async def events_take(team_id, checkin_id, now, row=None):
        state.events.append(checkin_id)
        return []

    async def fake_run(turn, header_line, checkin_id, team_id, kind):
        state.launched.append((turn, kind))

    from services.ai import events
    monkeypatch.setattr(spend, "reserve_autopilot", reserve)
    monkeypatch.setattr(spend, "cancel", cancel)
    monkeypatch.setattr(checkins, "_claim", claim)
    monkeypatch.setattr(asks, "take", take)
    monkeypatch.setattr(checkins, "_drop", drop)
    monkeypatch.setattr(conversations, "create", create)
    monkeypatch.setattr(conversations, "claim", conv_claim)
    monkeypatch.setattr(checkins, "_attach", attach)
    monkeypatch.setattr(agent, "header", header)
    monkeypatch.setattr(checkins, "_autopilot_row", autopilot_row)
    monkeypatch.setattr(events, "take", events_take)
    monkeypatch.setattr(checkins, "_run", fake_run)
    return state


def _start_ask():
    async def go():
        out = await checkins.start_ask(_row(), datetime(2026, 11, 22, 23, 31, tzinfo=timezone.utc))
        await asyncio.sleep(0)
        return out
    return run(go())


class TestStart:
    def test_it_takes_the_ask_and_runs_it_with_live_chips_and_no_events(self, starting):
        assert _start_ask() == 7
        assert starting.taken == [(4, 7)] and starting.events == []
        turn, kind = starting.launched[0]
        assert kind == "ask" and turn.live_chips is True and turn.checkin_id == 7
        assert turn.tools is agent.CHECKIN_TOOLS and turn.limits == agent.Limits.checkin()
        assert turn.message.startswith("[The user's ask for team_id 22")
        assert starting.created == [("checkin", "You asked: Start Herb Jones if Embiid sits")]

    def test_an_ask_taken_first_stands_down_and_leaves_no_row(self, starting):
        starting.take = False
        assert _start_ask() is None
        assert starting.dropped == [7] and starting.cancelled == [55] and starting.launched == []


class TestShadow:
    def test_a_check_in_s_proposals_are_shadow_unless_it_runs_an_ask(self):
        assert tools.ToolContext(user_id=1, checkin_id=3).shadow is True
        assert tools.ToolContext(user_id=1, checkin_id=3, live_chips=True).shadow is False
        assert tools.ToolContext(user_id=1).shadow is False


class TestLook:
    def test_off_on_the_server_a_look_reads_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_enabled", False)

        async def boom(now):
            raise AssertionError("read")
        monkeypatch.setattr(asks, "_due", boom)
        assert run(asks.run_due()) == {"off": True, "started": [], "waiting": 0, "closed": 0, "stale": 0}
