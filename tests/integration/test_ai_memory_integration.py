"""
Dime's notes against real SQL (usr.ai_memory, migration 0041): written, revised,
forgotten and read back the way the agent and the AI desk's "What Dime knows" do.
"""

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from core.errors import AppError, NotFoundError
from db.models.ai_conversations import AiConversation, AiMemory
from db.models.teams import Team
from db.models.users import User
from services.ai import memory

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 2)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def caller(integration_db):
    user = User.create(email="dime@courtvision.dev", clerk_user_id="user_dime", created_at=datetime.now(timezone.utc))
    info = {"provider": "espn", "league_id": 1001, "team_name": "Atlas", "year": 2027}
    team = Team.create(user_id=user.user_id, team_identifier="1001Atlas", league_info=json.dumps(info))
    conversation = AiConversation.create(user_id=user.user_id, team_id=team.team_id)
    return memory.Caller(user_id=user.user_id, conversation_id=conversation.id, today=TODAY), team


def test_a_note_is_revised_by_retiring_it_and_writing_its_replacement(caller):
    me, team = caller
    first = run(memory.remember(me, memory.RememberInput(kind="plan", team_id=team.team_id,
                                                         text="Punt FT% this matchup", recheck_on=date(2026, 11, 9))))
    revised = run(memory.revise(me, memory.ReviseInput(note_id=first["note_id"], text="Punt FT% and TO")))

    old = AiMemory.get_by_id(first["note_id"])
    new = AiMemory.get_by_id(revised["note_id"])
    assert (old.retired_by, old.retired_reason) == ("dime", "revised")
    assert (new.replaces_id, new.kind, new.recheck_on, new.conversation_id_id) == (
        old.id, "plan", date(2026, 11, 9), me.conversation_id)

    live = run(memory.recall(me, memory.RecallInput(team_id=team.team_id)))
    assert [n["text"] for n in live["notes"]] == ["Punt FT% and TO"]
    every = run(memory.recall(me, memory.RecallInput(team_id=team.team_id, include_retired=True)))
    assert [n["text"] for n in every["notes"]] == ["Punt FT% and TO", "Punt FT% this matchup"]   # live first
    assert run(memory.recall(me, memory.RecallInput(query="punt to")))["notes"][0]["text"] == "Punt FT% and TO"


def test_the_user_s_own_notes_outrank_dime_s(caller):
    me, team = caller
    note = run(memory.add_for_user(me.user_id, kind="preference", text="Never drop my rookies", team_id=None,
                                   recheck_on=None))
    with pytest.raises(AppError):
        run(memory.forget(me, memory.ForgetInput(note_id=note["note_id"], reason="stale")))
    edited = run(memory.edit_for_user(me.user_id, note["note_id"], text="Never drop rookies before December",
                                      kind=None, recheck_on=None, clear_recheck=False))
    assert edited["author"] == "user"
    run(memory.delete_for_user(me.user_id, edited["note_id"]))
    with pytest.raises(NotFoundError):
        run(memory.delete_for_user(me.user_id, edited["note_id"]))
    assert run(memory.list_for_user(me.user_id, None)) == []


def test_the_digest_reads_live_plans_and_due_rechecks(caller):
    me, team = caller
    run(memory.remember(me, memory.RememberInput(kind="plan", team_id=team.team_id, text="Stream bigs for boards")))
    run(memory.remember(me, memory.RememberInput(kind="player", team_id=team.team_id, text="Hold the rookie guard",
                                                 recheck_on=TODAY)))
    line = run(memory.digest(me.user_id, TODAY))
    assert "Stream bigs for boards" in line and "Hold the rookie guard" in line
