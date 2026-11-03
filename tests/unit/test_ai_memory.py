"""Dime's memory (services/ai/memory.py): the notebook the agent keeps between conversations."""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services.ai import agent, memory, tools

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 2)


class Store:
    """usr.ai_memory in a list, behind the repository functions the module calls."""

    def __init__(self):
        self.rows: list[SimpleNamespace] = []

    def add(self, **fields) -> SimpleNamespace:
        row = SimpleNamespace(
            id=len(self.rows) + 1, user_id_id=fields.pop("user_id", 42), team_id_id=fields.pop("team_id", None),
            kind=fields.pop("kind", "note"), text=fields.pop("text", "a note"), recheck_on=fields.pop("recheck_on", None),
            author=fields.pop("author", "dime"), conversation_id=fields.pop("conversation_id", None),
            replaces_id=fields.pop("replaces_id", None),
            created_at=fields.pop("created_at", datetime(2026, 11, 1, 12, tzinfo=timezone.utc) + timedelta(minutes=len(self.rows))),
            retired_at=None, retired_by=None, retired_reason=None,
        )
        assert not fields, fields
        self.rows.append(row)
        return row

    def live(self, user_id):
        return [r for r in self.rows if r.user_id_id == user_id and r.retired_at is None]

    # the repository
    def select_notes(self, user_id, *, team_id=None, kind=None, query=None, include_retired=False, limit=30):
        rows = [r for r in self.rows if r.user_id_id == user_id and (include_retired or r.retired_at is None)]
        if team_id is not None:
            rows = [r for r in rows if r.team_id_id in (team_id, None)]
        if kind is not None:
            rows = [r for r in rows if r.kind == kind]
        for word in (query or "").split():
            rows = [r for r in rows if word.lower() in r.text.lower()]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    def count_live(self, user_id, team_id):
        return sum(1 for r in self.live(user_id) if r.team_id_id == team_id)

    def get_note(self, note_id, user_id):
        return next((r for r in self.rows if r.id == note_id and r.user_id_id == user_id), None)

    def insert_note(self, *, user_id, team_id, kind, text, recheck_on, author, conversation_id, replaces_id=None):
        return self.add(user_id=user_id, team_id=team_id, kind=kind, text=text, recheck_on=recheck_on, author=author,
                        conversation_id=conversation_id, replaces_id=replaces_id)

    def retire_note(self, note_id, user_id, *, by, reason):
        row = self.get_note(note_id, user_id)
        if row is None or row.retired_at is not None:
            return False
        row.retired_at, row.retired_by, row.retired_reason = datetime.now(timezone.utc), by, reason
        return True

    def replace_note(self, old, *, kind, text, recheck_on, author, conversation_id):
        if not self.retire_note(old.id, old.user_id_id, by=author, reason="revised"):
            return None
        return self.insert_note(user_id=old.user_id_id, team_id=old.team_id_id, kind=kind, text=text,
                                recheck_on=recheck_on, author=author, conversation_id=conversation_id,
                                replaces_id=old.id)

    def live_for_digest(self, user_id):
        return sorted(self.live(user_id), key=lambda r: r.created_at, reverse=True)


@pytest.fixture
def store(monkeypatch):
    s = Store()
    for name in ("select_notes", "count_live", "get_note", "insert_note", "retire_note", "replace_note",
                 "live_for_digest"):
        monkeypatch.setattr(memory, name, getattr(s, name))

    async def run_db(_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(memory, "run_db", run_db)
    return s


CALLER = memory.Caller(user_id=42, conversation_id=7, today=TODAY)


def _run(coro):
    return asyncio.run(coro)


class TestRemember:
    def test_a_note_is_the_agent_s_and_names_its_conversation(self, store):
        out = _run(memory.remember(CALLER, memory.RememberInput(kind="plan", text="  Punt   FT%; chase Saturday volume ",
                                                                team_id=21)))
        row = store.rows[0]
        assert out == {"note_id": row.id, "live_notes": 1, "cap": memory.CAP}
        assert (row.author, row.team_id_id, row.conversation_id, row.text) == ("dime", 21, 7,
                                                                              "Punt FT%; chase Saturday volume")

    def test_a_full_team_refuses_until_a_note_goes(self, store):
        for _ in range(memory.CAP):
            store.add(team_id=21)
        store.add(team_id=22)            # another team's notes don't count
        with pytest.raises(memory.AppError, match="60 live notes"):
            _run(memory.remember(CALLER, memory.RememberInput(kind="note", text="one more", team_id=21)))
        _run(memory.remember(CALLER, memory.RememberInput(kind="note", text="about the user")))

    @pytest.mark.parametrize("days", [-1, memory.RECHECK_MAX_DAYS + 1])
    def test_a_recheck_date_is_ahead_and_within_reach(self, store, days):
        with pytest.raises(memory.AppError):
            _run(memory.remember(CALLER, memory.RememberInput(kind="player", text="Hold him",
                                                              recheck_on=TODAY + timedelta(days=days))))
        assert store.rows == []


class TestReviseAndForget:
    def test_revising_retires_the_old_note_and_links_the_new_one(self, store):
        old = store.add(team_id=21, kind="player", text="Hold him until his minutes return", recheck_on=TODAY)
        out = _run(memory.revise(CALLER, memory.ReviseInput(note_id=old.id, text="Drop him if he's out past Nov 10",
                                                            recheck_on=TODAY + timedelta(days=8))))
        new = store.rows[-1]
        assert out == {"note_id": new.id, "replaced": old.id}
        assert (old.retired_by, old.retired_reason) == ("dime", "revised")
        assert (new.replaces_id, new.kind, new.team_id_id, new.recheck_on) == (old.id, "player", 21, date(2026, 11, 10))

    def test_clear_recheck_drops_the_date_and_nothing_new_is_refused(self, store):
        old = store.add(text="x", recheck_on=TODAY)
        with pytest.raises(memory.AppError, match="Nothing to change"):
            _run(memory.revise(CALLER, memory.ReviseInput(note_id=old.id, text="x")))
        _run(memory.revise(CALLER, memory.ReviseInput(note_id=old.id, clear_recheck=True)))
        assert store.rows[-1].recheck_on is None

    def test_the_user_s_notes_are_not_the_agent_s_to_change(self, store):
        mine = store.add(author="user", text="Never drop Wemby")
        with pytest.raises(memory.AppError, match="user's own"):
            _run(memory.revise(CALLER, memory.ReviseInput(note_id=mine.id, text="Drop Wemby")))
        with pytest.raises(memory.AppError, match="user's own"):
            _run(memory.forget(CALLER, memory.ForgetInput(note_id=mine.id, reason="no longer true")))
        assert mine.retired_at is None

    def test_forgetting_retires_with_the_reason_and_twice_is_refused(self, store):
        old = store.add(text="Chase blocks this week")
        assert _run(memory.forget(CALLER, memory.ForgetInput(note_id=old.id, reason="matchup  over"))) == {"forgotten": old.id}
        assert (old.retired_by, old.retired_reason) == ("dime", "matchup over")
        with pytest.raises(memory.AppError, match="already forgotten"):
            _run(memory.forget(CALLER, memory.ForgetInput(note_id=old.id, reason="again")))

    def test_another_user_s_note_is_not_found(self, store):
        theirs = store.add(user_id=99)
        with pytest.raises(memory.AppError, match="No note"):
            _run(memory.forget(CALLER, memory.ForgetInput(note_id=theirs.id, reason="mine now")))


class TestRecall:
    def test_a_team_reads_its_notes_and_the_user_s_newest_first(self, store):
        store.add(team_id=21, text="Punt FT%", kind="plan")
        store.add(team_id=22, text="Other team")
        store.add(text="Likes to stream on Sundays", kind="preference", author="user")
        out = _run(memory.recall(CALLER, memory.RecallInput(team_id=21)))
        assert [n["text"] for n in out["notes"]] == ["Likes to stream on Sundays", "Punt FT%"]
        assert out["notes"][0]["author"] == "user" and out["live_on_team"] == 1

    def test_query_and_retired(self, store):
        gone = store.add(text="Hold Clarkson")
        store.retire_note(gone.id, 42, by="dime", reason="revised")
        store.add(text="Hold Sexton")
        assert [n["text"] for n in _run(memory.recall(CALLER, memory.RecallInput(query="hold")))["notes"]] == ["Hold Sexton"]
        both = _run(memory.recall(CALLER, memory.RecallInput(query="clarkson", include_retired=True)))["notes"]
        assert both[0]["retired_reason"] == "revised" and both[0]["retired_by"] == "dime"
        assert _run(memory.recall(CALLER, memory.RecallInput(query="nobody")))["note"] == "No notes match."


class TestDigest:
    def test_no_notes_no_digest(self):
        assert memory.digest_of([], TODAY) == ""

    def test_it_carries_the_plans_and_the_notes_due(self, store):
        store.add(team_id=21, kind="plan", text="Old plan")
        store.add(team_id=21, kind="plan", text='Punt FT%; "chase" Saturday volume')
        store.add(team_id=21, kind="plan", text="Newest plan")
        store.add(kind="player", text="Recheck Clarkson's minutes", recheck_on=TODAY, team_id=21)
        store.add(kind="player", text="Not yet", recheck_on=TODAY + timedelta(days=3))
        line = memory.digest_of(store.live_for_digest(42), TODAY)
        assert line.startswith("Your notes: 5 live (recall reads them).")
        # The two newest plans per team, quotes made safe; only notes whose date has come
        assert "Plans: team_id 21: \"Newest plan\"; \"Punt FT%; 'chase' Saturday volume\"." in line
        assert "Old plan" not in line and "Not yet" not in line
        assert line.endswith("Due for a recheck: note 4 (team_id 21) \"Recheck Clarkson's minutes\".")

    def test_a_long_note_is_cut(self, store):
        store.add(kind="plan", text="x" * 400)
        assert "x" * memory.DIGEST_TEXT not in memory.digest_of(store.live_for_digest(42), TODAY)


def test_the_header_carries_the_digest_between_the_teams_and_the_proposals(monkeypatch):
    async def teams(user_id):
        return []

    async def season():
        return "Season 2026-27."

    async def digest(user_id, today):
        assert today == date(2026, 11, 2)
        return "Your notes: 1 live (recall reads them)."

    async def chips(conversation_id, user_id, now):
        return "Earlier proposals: none."

    monkeypatch.setattr(agent, "list_my_teams", teams)
    monkeypatch.setattr(agent, "_season_line", season)
    monkeypatch.setattr(memory, "digest", digest)
    monkeypatch.setattr(agent.proposals, "header_line", chips)
    line = _run(agent.header(42, None, datetime(2026, 11, 2, 15, tzinfo=timezone.utc), conversation_id=7))
    assert line.endswith("The user has no teams yet. Your notes: 1 live (recall reads them). Earlier proposals: none.]")

    async def broken(user_id, today):
        raise RuntimeError("db down")

    monkeypatch.setattr(memory, "digest", broken)
    assert "Your notes" not in _run(agent.header(42, None, conversation_id=7))


def test_a_note_s_numbers_never_ground_an_answer():
    assert {"recall", "remember", "revise", "forget", "find_in_app"} <= agent.NOT_SOURCES
    # The check itself: a number only a note holds is still made up
    note = {"notes": [{"text": "He averages 41 fantasy points"}]}
    assert agent.ungrounded_numbers("He averages 41.", [], set()) == 1
    assert agent.ungrounded_numbers("He averages 41.", [note], set()) == 0   # what NOT_SOURCES keeps out


class TestTheTools:
    """Through run_tool: the agent's toolset, the owned-team check, errors the model reads."""

    @pytest.fixture
    def owned(self, monkeypatch):
        async def run_db(_name, fn, team_id, user_id):
            return SimpleNamespace(team_id=team_id) if team_id == 21 else None

        monkeypatch.setattr(tools, "run_db", run_db)
        monkeypatch.setattr(tools, "nba_date_et", lambda: TODAY)

    def _call(self, name, raw):
        return _run(tools.run_tool(name, raw, ctx=tools.ToolContext(user_id=42, conversation_id=7),
                                   allowed=tools.AGENT_TOOL_NAMES))

    def test_they_are_the_agent_s_alone(self):
        assert memory.MEMORY_TOOL_NAMES <= tools.AGENT_TOOL_NAMES
        assert not memory.MEMORY_TOOL_NAMES & (tools.ASK_TOOL_NAMES | tools.ROUTER_TOOL_NAMES)
        for t in memory.MEMORY_TOOLS:
            assert t["input_schema"]["additionalProperties"] is False

    def test_a_team_that_isn_t_the_user_s_is_refused_before_any_write(self, store, owned):
        out = self._call("remember", {"kind": "plan", "text": "Punt FT%", "team_id": 99})
        assert out.is_error and "not one of the user's teams" in out.content and store.rows == []
        ok = self._call("remember", {"kind": "plan", "text": "Punt FT%", "team_id": 21, "recheck_on": "2026-11-09"})
        assert not ok.is_error and json.loads(ok.content)["note_id"] == 1
        assert store.rows[0].recheck_on == date(2026, 11, 9)

    def test_bad_input_is_an_error_the_model_reads(self, store, owned):
        assert self._call("remember", {"kind": "gossip", "text": "x"}).is_error
        assert self._call("remember", {"kind": "note", "text": "   "}).is_error
        assert self._call("forget", {"note_id": 1}).is_error


@pytest.mark.parametrize("text", ["   ", "\n\t ", "a\x00b"])
def test_a_blank_or_nul_note_is_refused_at_the_schema(text):
    from pydantic import ValidationError

    from schemas.ai import AiNoteCreateReq, AiNoteEditReq

    with pytest.raises(ValidationError):
        AiNoteCreateReq(text=text)
    with pytest.raises(ValidationError):
        AiNoteEditReq(text=text)


def test_a_note_with_words_and_lines_passes_the_schema():
    from schemas.ai import AiNoteCreateReq, AiNoteEditReq

    assert AiNoteCreateReq(text="  Keep my rookies\nuntil December ").text.strip().startswith("Keep")
    assert AiNoteEditReq(text="x").text == "x"
    assert AiNoteEditReq().text is None


def test_a_number_only_in_the_note_digest_is_not_grounded():
    header = ('[Today is Monday 2026-11-02 (US Eastern). The user\'s teams: team_id 7 (espn, points scoring, '
              '2026-27). Your notes: 2 live (recall reads them). Plans: team_id 7: "stream 3 adds before Friday". '
              'Due for a recheck: note 12 "he averages 41 lately".]')
    content = f"{header}\n\nWho should I add? I have \"4 adds\" left."
    cleaned = memory.without_note_texts(content)
    assert '"stream 3 adds before Friday"' not in cleaned and "averages 41" not in cleaned
    assert "team_id 7" in cleaned and "note 12" in cleaned               # ids stay sources
    assert cleaned.endswith('Who should I add? I have "4 adds" left.')  # the user's own words stay
    assert agent.ungrounded_numbers("He averages 41.", [cleaned], set()) == 1
    assert agent.ungrounded_numbers("You have 4 adds.", [cleaned], set()) == 0


def test_a_message_without_a_header_is_left_alone():
    text = 'Your notes: "41 points"'
    assert memory.without_note_texts(text) == text
