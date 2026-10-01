"""
Integration: the AI router against the real schema (migration 0026).

The question log -- recording, the 90-day text retention that runs with each
insert and at startup, owner-scoped feedback, the cascade from a deleted user --
and the one lookup that validates a routed destination: players that exist, NBA
teams that exist, and fantasy teams that belong to the caller and nobody else.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from peewee import IntegrityError

from db.base import db
from db.models.ai_questions import AiQuestion
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from db.models.teams import Team
from db.models.users import User
from services.ai import questions, routing

pytestmark = [pytest.mark.integration]


def _user(n):
    return User.create(email=f"ai{n}@courtvision.dev", clerk_user_id=f"user_ai_{n}", created_at=datetime.now(timezone.utc))


def _team(user, name):
    info = {"provider": "espn", "league_id": 1234, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"1234{name}", league_info=json.dumps(info))


def _row(user, **overrides):
    return {"user_id": user.user_id, "question": "sengun last 15", "context": {"mode": "player"},
            "kind": "show", "target": {"type": "terminal", "mode": "player", "player_id": 1630578},
            "tool_calls": [{"name": "search_players", "input": {"name": "Sengun"}, "is_error": False}],
            "outcome": "ok", "model_calls": 2, "input_tokens": 900, **overrides}


def record(**row):
    return asyncio.run(questions.record(**row))


class TestQuestionLog:
    def test_a_question_is_stored_as_sent(self, integration_db):
        user = _user(1)

        question_id = record(**_row(user))

        stored = AiQuestion.get_by_id(question_id)
        assert (stored.question, stored.kind, stored.outcome, stored.model_calls) == ("sengun last 15", "show", "ok", 2)
        assert stored.context == {"mode": "player"}
        assert stored.tool_calls[0]["name"] == "search_players"
        assert stored.feedback is None

    def test_the_schema_refuses_unknown_kinds_and_feedback(self, integration_db):
        user = _user(2)
        for bad in ({"kind": "tell"}, {"feedback": "meh"}):
            with pytest.raises(IntegrityError), db.atomic():
                AiQuestion.insert(**_row(user, **bad)).execute()

    def test_text_older_than_ninety_days_is_redacted_on_the_next_insert(self, integration_db):
        user = _user(3)
        now = datetime.now(timezone.utc)
        old = AiQuestion.insert(**_row(user, created_at=now - timedelta(days=91))).execute()
        recent = AiQuestion.insert(**_row(user, created_at=now - timedelta(days=10))).execute()

        record(**_row(user, question="who leads in blocks"))

        old_row, recent_row = AiQuestion.get_by_id(old), AiQuestion.get_by_id(recent)
        assert (old_row.question, old_row.context) == (None, None)
        assert (old_row.kind, old_row.input_tokens) == ("show", 900)  # the counts stay
        assert recent_row.question == "sengun last 15"

    def test_the_startup_sweep_redacts_without_a_new_question(self, integration_db):
        """Riding on inserts alone, the policy lapsed whenever AI traffic stopped."""
        user = _user(9)
        now = datetime.now(timezone.utc)
        old = AiQuestion.insert(**_row(user, created_at=now - timedelta(days=91))).execute()
        recent = AiQuestion.insert(**_row(user, created_at=now - timedelta(days=89))).execute()

        asyncio.run(questions.redact_expired())

        old_row, recent_row = AiQuestion.get_by_id(old), AiQuestion.get_by_id(recent)
        assert (old_row.question, old_row.context, old_row.kind) == (None, None, "show")
        assert (recent_row.question, recent_row.context) == ("sengun last 15", {"mode": "player"})
        assert AiQuestion.select().count() == 2

    def test_a_failing_sweep_does_not_cost_the_insert_it_rides_on(self, integration_db, monkeypatch):
        user = _user(10)

        def failing_sweep():
            db.execute_sql("SELECT 1 / 0")
        monkeypatch.setattr(questions, "_redact_expired", failing_sweep)

        first = record(**_row(user))
        second = record(**_row(user, question="who leads in blocks"))  # and the connection is still good

        assert first is not None and second is not None
        assert [q.question for q in AiQuestion.select().order_by(AiQuestion.id)] == ["sengun last 15", "who leads in blocks"]

    def test_a_nul_character_cannot_keep_a_question_out_of_the_log(self, integration_db):
        """Postgres text and jsonb refuse NUL; the insert failed and the question went unrecorded."""
        user = _user(11)

        question_id = record(**_row(
            user, question="sengun\x00 last 15", context={"page": "ter\x00minal"}, missing="career\x00 splits",
            target={"type": "terminal", "mode": "player", "window": "l15\x00"},
            tool_calls=[{"name": "search_players", "input": {"name": "Sen\x00gun"}, "is_error": False}]))

        stored = AiQuestion.get_by_id(question_id)
        assert (stored.question, stored.context, stored.missing) == ("sengun last 15", {"page": "terminal"}, "career splits")
        assert stored.target["window"] == "l15"
        assert stored.tool_calls[0]["input"] == {"name": "Sengun"}

    def test_feedback_is_the_askers_alone(self, integration_db):
        asker, other = _user(4), _user(5)
        question_id = record(**_row(asker))

        assert asyncio.run(questions.set_feedback(question_id, other.user_id, "down")) is False
        assert asyncio.run(questions.set_feedback(question_id, asker.user_id, "up")) is True
        assert AiQuestion.get_by_id(question_id).feedback == "up"
        assert asyncio.run(questions.set_feedback(question_id, asker.user_id, None)) is True
        assert AiQuestion.get_by_id(question_id).feedback is None

    def test_deleting_a_user_deletes_their_questions(self, integration_db):
        user = _user(6)
        question_id = record(**_row(user))

        User.delete().where(User.user_id == user.user_id).execute()

        assert AiQuestion.get_or_none(AiQuestion.id == question_id) is None


class TestTargetLookup:
    def test_finds_what_exists_and_only_the_callers_teams(self, integration_db):
        Player.create(id=1630578, espn_id=4871144, name="Alperen Sengun", name_normalized="alperen sengun")
        NBATeam.insert(id="HOU", name="Houston Rockets", conference="West", division="Southwest").on_conflict_ignore().execute()
        asker, other = _user(7), _user(8)
        mine, theirs = _team(asker, "Mine"), _team(other, "Theirs")

        found = asyncio.run(routing._lookup(
            [1630578, 999999], [mine.team_id, theirs.team_id], ["HOU", "XYZ"], asker.user_id))

        assert found.players == {1630578}
        assert found.owned_teams == {mine.team_id}
        assert found.nba_teams == {"HOU"}

    def test_view_names_come_from_our_own_tables(self, integration_db):
        Player.create(id=1627734, espn_id=3155942, name="Domantas Sabonis", name_normalized="domantas sabonis")
        NBATeam.insert(id="SAC", name="Sacramento Kings", conference="West", division="Pacific").on_conflict_ignore().execute()

        names, team = asyncio.run(routing._view_names([1627734, 999999], "SAC"))

        assert names == {1627734: "Domantas Sabonis"}
        assert team == "Sacramento Kings"
