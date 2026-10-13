"""
Integration: the AI agent's conversations against the real schema (migration 0032).

The claim that keeps two turns off one conversation, the history a new turn
replays (finished turns only, in order, never edited), the monthly spend sums
the caps read, and the scoping: another user's conversation reads as missing.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from db.models.ai_conversations import AiConversation, AiTurn
from db.models.teams import Team
from db.models.users import User
from services.ai import conversations, spend

pytestmark = [pytest.mark.integration]

HOLD = timedelta(seconds=180)


def _user(n):
    return User.create(email=f"agent{n}@courtvision.dev", clerk_user_id=f"user_agent_{n}",
                       created_at=datetime.now(timezone.utc))


def _team(user):
    info = {"provider": "espn", "league_id": 1234, "team_name": "Mine", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="1234Mine", league_info=json.dumps(info))


def run(coro):
    return asyncio.run(coro)


def _turn(user, n, outcome="ok", cost=1000, **extra):
    messages = [{"role": "user", "content": f"q{n}"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": f"s{n}"},
                                                  {"type": "text", "text": f"a{n}"}]}]
    return {"user_id": user.user_id, "message": f"q{n}", "outcome": outcome,
            "messages": messages if outcome == "ok" else None,
            "results": {f"r{n}": {"tool": "get_matchup", "input": {}, "data": {"n": n}}} if outcome == "ok" else {},
            "parts": [{"kind": "text", "text": f"a{n}"}], "cost_micros": cost, "context_tokens": 1000 * n, **extra}


class TestConversations:
    def test_scoped_to_their_user(self, integration_db):
        me, other = _user(1), _user(2)
        conv = run(conversations.create(me.user_id, None))

        assert run(conversations.get(conv.id, me.user_id)).id == conv.id
        assert run(conversations.get(conv.id, other.user_id)) is None
        assert run(conversations.delete(conv.id, other.user_id)) is False
        assert run(conversations.claim(conv.id, other.user_id, HOLD)) is False

    def test_one_turn_at_a_time_and_a_lapsed_claim_frees_itself(self, integration_db):
        me = _user(1)
        conv = run(conversations.create(me.user_id, None))

        assert run(conversations.claim(conv.id, me.user_id, HOLD)) is True
        assert run(conversations.claim(conv.id, me.user_id, HOLD)) is False
        run(conversations.release(conv.id))
        assert run(conversations.claim(conv.id, me.user_id, HOLD)) is True

        AiConversation.update(busy_until=datetime.now(timezone.utc) - timedelta(seconds=1)).execute()
        assert run(conversations.claim(conv.id, me.user_id, HOLD)) is True

    def test_writing_a_turn_releases_the_claim_and_names_the_conversation_once(self, integration_db):
        me = _user(1)
        conv = run(conversations.create(me.user_id, None))
        run(conversations.claim(conv.id, me.user_id, HOLD))

        run(conversations.record(conv.id, _turn(me, 1), title="first question"))
        run(conversations.record(conv.id, _turn(me, 2), title="second question"))

        stored = AiConversation.get_by_id(conv.id)
        assert stored.busy_until is None
        assert stored.title == "first question"
        assert run(conversations.claim(conv.id, me.user_id, HOLD)) is True

    def test_history_is_the_finished_turns_in_order(self, integration_db):
        me = _user(1)
        conv = run(conversations.create(me.user_id, None))
        for n, outcome in [(1, "ok"), (2, "AI_TIMEOUT"), (3, "ok")]:
            run(conversations.record(conv.id, _turn(me, n, outcome)))

        history = run(conversations.history(conv.id, me.user_id))

        assert [m["content"] for m in history.messages if m["role"] == "user"] == ["q1", "q3"]
        # Thinking blocks come back byte for byte, signature included
        assert history.messages[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s1"}
        assert set(history.results) == {"r1", "r3"}
        assert (history.turns, history.context_tokens) == (2, 3000)
        # The failed turn is still listed, and still cost money
        assert [t.outcome for t in run(conversations.turns_of(conv.id, me.user_id))] == ["ok", "AI_TIMEOUT", "ok"]

    def test_a_conversation_fills_up(self, integration_db, monkeypatch):
        from core.settings import settings
        monkeypatch.setattr(settings, "ai_agent_max_turns", 2)
        me = _user(1)
        conv = run(conversations.create(me.user_id, None))
        run(conversations.record(conv.id, _turn(me, 1)))
        assert run(conversations.history(conv.id, me.user_id)).full is False
        run(conversations.record(conv.id, _turn(me, 2)))
        assert run(conversations.history(conv.id, me.user_id)).full is True

    def test_nul_is_stripped_before_it_reaches_jsonb(self, integration_db):
        me = _user(1)
        conv = run(conversations.create(me.user_id, None))

        turn_id = run(conversations.record(conv.id, _turn(me, 1, parts=[{"kind": "text", "text": "a\x00b"}])))

        assert AiTurn.get_by_id(turn_id).parts == [{"kind": "text", "text": "ab"}]

    def test_listing_counts_turns_and_deleting_takes_them_along(self, integration_db):
        me = _user(1)
        a = run(conversations.create(me.user_id, None))
        b = run(conversations.create(me.user_id, None))
        run(conversations.record(a.id, _turn(me, 1)))
        run(conversations.record(a.id, _turn(me, 2)))

        listed = run(conversations.list_for(me.user_id))
        assert [(item["conversation"].id, item["turns"]) for item in listed] == [(a.id, 2), (b.id, 0)]

        assert run(conversations.delete(a.id, me.user_id)) is True
        assert AiTurn.select().count() == 0

    def test_a_deleted_team_leaves_the_conversation_unpinned(self, integration_db):
        me = _user(1)
        team = _team(me)
        conv = run(conversations.create(me.user_id, team.team_id))

        team.delete_instance()

        assert AiConversation.get_by_id(conv.id).team_id_id is None

    def test_feedback_is_the_caller_s_own(self, integration_db):
        me, other = _user(1), _user(2)
        conv = run(conversations.create(me.user_id, None))
        turn_id = run(conversations.record(conv.id, _turn(me, 1)))

        assert run(conversations.set_feedback(turn_id, other.user_id, "down")) is False
        assert run(conversations.set_feedback(turn_id, me.user_id, "up")) is True
        assert AiTurn.get_by_id(turn_id).feedback == "up"


class TestMonthlySpend:
    def test_sums_this_month_per_user_and_for_everyone(self, integration_db, monkeypatch):
        from core.settings import settings
        monkeypatch.setattr(settings, "ai_agent_user_ids", [1, 2])
        me, other = _user(1), _user(2)
        mine = run(conversations.create(me.user_id, None))
        theirs = run(conversations.create(other.user_id, None))
        run(conversations.record(mine.id, _turn(me, 1, cost=40_000)))
        run(conversations.record(mine.id, _turn(me, 2, outcome="AI_TIMEOUT", cost=10_000)))
        run(conversations.record(theirs.id, _turn(other, 1, cost=5_000)))
        last_month = spend.month_start() - timedelta(days=1)
        run(conversations.record(mine.id, _turn(me, 3, cost=999_000, created_at=last_month)))

        s = run(spend.month_spend(me.user_id))

        assert (s.user, s.total) == (50_000, 55_000)
        assert s.user_cap == 50_000_000
