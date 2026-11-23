"""
Integration: the AI spend report and the caps set from the dashboard, against the real
schema (migrations 0032, 0042, 0053, 0054).

The caps: a row replaces the environment's number from the next reservation on, a
user's own row wins over their share, clearing one hands the number back, and the
analyst has no users. The report: each ledger row lands under what spent it (a chat,
the draft room, a check-in by kind, a reply in a check-in's conversation, an analyst
read, or nothing left to say once its chat is deleted), open reservations are counted
apart, and usage carries each record's model and tokens.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.errors import AppError, BadRequestError, NotFoundError
from db.base import db
from db.models.ai_conversations import AiCap, AiSpend
from db.models.autopilot import AiCheckin
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User
from schemas.ai_spend import AiCapReq
from services.ai import conversations, spend, spend_report

pytestmark = [pytest.mark.integration]

NOW = datetime.now(timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _user(n):
    return User.create(email=f"spend{n}@courtvision.dev", clerk_user_id=f"user_spend_{n}", created_at=NOW)


def _team(user, name="Mine"):
    info = {"provider": "espn", "league_id": 1234, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"1234{name}", league_info=json.dumps(info))


def _turn(user, cost, model="claude-opus-5-5", **tokens):
    return {"user_id": user.user_id, "message": "q", "outcome": "ok",
            "messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            "results": {}, "parts": [], "cost_micros": cost, "model": model, "model_calls": 2, **tokens}


@pytest.fixture
def caps(monkeypatch):
    from core.settings import settings
    monkeypatch.setattr(settings, "ai_agent_user_ids", [1, 2])
    monkeypatch.setattr(settings, "ai_monthly_budget_usd", 10.0)
    monkeypatch.setattr(settings, "autopilot_monthly_budget_usd", 6.0)
    monkeypatch.setattr(settings, "analyst_monthly_budget_usd", 4.0)
    monkeypatch.setattr(settings, "ai_agent_turn_max_usd", 1.0)
    monkeypatch.setattr(settings, "analyst_read_max_usd", 0.30)


def _set(line, usd, user=None, author="James"):
    return run(spend_report.set_cap(AiCapReq(line=line, user_id=user.user_id if user else None,
                                             monthly_usd=None if usd is None else Decimal(str(usd)),
                                             author=author)))


class TestCaps:
    def test_a_desk_cap_replaces_the_environment_s_and_moves_every_share(self, integration_db, caps):
        me = _user(1)
        _user(2)
        assert run(spend.month_spend(me.user_id)).user_cap == 5_000_000

        data = _set("desk", 20)

        assert (data.cap_micros, data.default_cap_micros, data.cap_set, data.updated_by) == (
            20_000_000, 10_000_000, True, "James")
        s = run(spend.month_spend(me.user_id))
        assert (s.user_cap, s.total_cap) == (10_000_000, 20_000_000)

    def test_a_user_s_own_desk_cap_is_what_refuses_their_next_turn(self, integration_db, caps):
        me, other = _user(1), _user(2)
        _set("desk", 2, me)
        run(spend.reserve(me.user_id))
        run(spend.reserve(me.user_id))

        with pytest.raises(AppError) as err:
            run(spend.reserve(me.user_id))
        assert err.value.error_code == "AI_USER_BUDGET_REACHED"
        assert run(spend.reserve(other.user_id))  # their share is still $5

    def test_clearing_a_cap_hands_the_number_back(self, integration_db, caps):
        me = _user(1)
        _set("desk", 2, me)

        data = _set("desk", None, me)

        assert (data.cap_micros, data.cap_set) == (5_000_000, False)
        assert AiCap.select().count() == 0
        assert run(spend.month_spend(me.user_id)).user_cap == 5_000_000

    def test_setting_a_cap_again_updates_its_row(self, integration_db, caps):
        _set("autopilot", 3, author="first")
        _set("autopilot", 4.5, author="second")

        row = AiCap.get()
        assert (row.line, row.user_id_id, row.monthly_usd, row.updated_by) == (
            "autopilot", None, Decimal("4.50"), "second")
        assert run(spend.autopilot_month_spend()) == (0, 4_500_000)

    def test_a_user_s_autopilot_cap_skips_their_check_ins_alone(self, integration_db, caps):
        me, other = _user(1), _user(2)
        _set("autopilot", 1, me)

        assert run(spend.reserve_autopilot(me.user_id)) is not None
        assert run(spend.reserve_autopilot(me.user_id)) is None
        assert run(spend.reserve_autopilot(other.user_id)) is not None

    def test_a_line_cap_of_zero_stops_the_analyst(self, integration_db, caps):
        _set("analyst", 0)

        assert run(spend.reserve_analyst()) is None
        assert run(spend.analyst_month_spend()) == (0, 0)

    def test_the_analyst_has_no_users_and_an_unknown_user_is_not_found(self, integration_db, caps):
        me = _user(1)
        with pytest.raises(BadRequestError):
            _set("analyst", 1, me)
        with pytest.raises(NotFoundError):
            run(spend_report.set_cap(AiCapReq(line="desk", user_id=999, monthly_usd=Decimal("1"))))
        assert AiCap.select().count() == 0

    def test_a_deleted_user_takes_their_caps_along(self, integration_db, caps):
        me = _user(1)
        _set("desk", 2, me)
        _set("desk", 30)

        me.delete_instance()

        assert [(r.line, r.user_id_id) for r in AiCap.select()] == [("desk", None)]


class TestReport:
    def _book(self):
        """One of everything this month, and one row before the window."""
        me, other = _user(1), _user(2)
        mine, theirs = _team(me, "Mine"), _team(other, "Theirs")

        chat = run(conversations.create(me.user_id, mine.team_id))
        run(conversations.record(chat.id, _turn(me, 40_000, input_tokens=1_000, output_tokens=200,
                                                cache_read_input_tokens=9_000),
                                 reservation_id=run(spend.reserve(me.user_id))))
        draft = run(conversations.create(me.user_id, None, kind=conversations.DRAFT))
        run(conversations.record(draft.id, _turn(me, 30_000), reservation_id=run(spend.reserve(me.user_id))))

        checkin_conv = run(conversations.create(other.user_id, theirs.team_id, kind=conversations.CHECKIN))
        checkin_turn = run(conversations.record(checkin_conv.id, _turn(other, 70_000, model="claude-opus-5"),
                                                reservation_id=run(spend.reserve_autopilot(other.user_id))))
        AiCheckin.create(team_id=theirs.team_id, user_id=other.user_id, kind="morning", nba_date=NOW.date(),
                         due_at=NOW, state="done", conversation_id=checkin_conv.id, turn_id=checkin_turn,
                         cost_micros=70_000)
        run(conversations.record(checkin_conv.id, _turn(other, 20_000),
                                 reservation_id=run(spend.reserve(other.user_id))))

        settled_read = run(spend.reserve_analyst())
        run(spend.settle(settled_read, 25_000))
        run(spend.reserve_analyst())                       # still open: counted at its $0.30 ceiling
        run(spend.reserve(other.user_id))                  # a desk turn still running: $1 open

        gone = run(conversations.create(me.user_id, None))
        run(conversations.record(gone.id, _turn(me, 5_000), reservation_id=run(spend.reserve(me.user_id))))
        run(conversations.delete(gone.id, me.user_id))

        AiSpend.create(user_id=me.user_id, cost_micros=999_000, settled=True, created_at=NOW - timedelta(days=60))
        return me, other, mine, theirs

    def test_each_row_lands_under_what_spent_it(self, integration_db, caps):
        me, other, mine, theirs = self._book()

        r = run(spend_report.report(30, now=NOW))

        got = sorted((row.line, row.feature, row.detail, row.user_id, row.team_id, row.cost_micros, row.open_micros)
                     for row in r.rows)
        assert got == sorted([
            ("desk", "chat", None, me.user_id, mine.team_id, 40_000, 0),
            ("desk", "draft", None, me.user_id, None, 30_000, 0),
            ("desk", "unlinked", None, me.user_id, None, 5_000, 0),
            ("desk", "checkin_reply", None, other.user_id, theirs.team_id, 20_000, 0),
            ("desk", "unlinked", None, other.user_id, None, 1_000_000, 1_000_000),
            ("autopilot", "checkin", "morning", other.user_id, theirs.team_id, 70_000, 0),
            ("analyst", "analyst", None, None, None, 325_000, 300_000),
        ])
        assert {t.team_id: t.name for t in r.teams} == {mine.team_id: "Mine", theirs.team_id: "Theirs"}

    def test_the_month_against_each_cap(self, integration_db, caps):
        self._book()
        _set("analyst", 2)

        lines = {line.line: line for line in run(spend_report.report(30, now=NOW)).lines}

        assert (lines["desk"].spent_micros, lines["desk"].open_micros, lines["desk"].cap_micros) == (
            95_000, 1_000_000, 10_000_000)
        assert (lines["autopilot"].spent_micros, lines["autopilot"].cap_micros) == (70_000, 6_000_000)
        analyst = lines["analyst"]
        assert (analyst.spent_micros, analyst.open_micros, analyst.cap_micros, analyst.default_cap_micros,
                analyst.cap_set, analyst.updated_by) == (25_000, 300_000, 2_000_000, 4_000_000, True, "James")

    def test_users_carry_their_month_and_caps(self, integration_db, caps):
        me, other, _mine, _theirs = self._book()
        _set("autopilot", 3, other)
        outsider = _user(3)
        _set("desk", 1, outsider)

        users = {u.user_id: u for u in run(spend_report.report(30, now=NOW)).users}

        assert set(users) == {me.user_id, other.user_id, outsider.user_id}
        assert users[me.user_id].allowed and not users[outsider.user_id].allowed
        assert (users[me.user_id].desk.spent_micros, users[me.user_id].desk.cap_micros,
                users[me.user_id].desk.cap_set) == (75_000, 5_000_000, False)
        their_autopilot = users[other.user_id].autopilot
        assert (their_autopilot.spent_micros, their_autopilot.cap_micros, their_autopilot.default_cap_micros) == (
            70_000, 3_000_000, None)
        assert users[me.user_id].autopilot.cap_micros is None
        assert (users[outsider.user_id].desk.cap_micros, users[outsider.user_id].desk.default_cap_micros) == (
            1_000_000, 5_000_000)

    def test_usage_is_each_record_s_model_and_tokens(self, integration_db, caps):
        self._book()
        Player.create(id=7, name="P7", name_normalized="p7")
        db.execute_sql(
            "INSERT INTO nba.analyst_reads (season, as_of_date, player_id, reasons, fingerprint, outcome, model,"
            " input_tokens, output_tokens, cost_micros) VALUES ('2026-27', %s, 7, '[]', 'f', 'none',"
            " 'claude-opus-5-5', 3000, 400, 25000)", (NOW.date(),))

        usage = {(u.feature, u.model): u for u in run(spend_report.report(30, now=NOW)).usage}

        assert set(usage) == {("chat", "claude-opus-5-5"), ("draft", "claude-opus-5-5"),
                              ("checkin", "claude-opus-5"), ("checkin_reply", "claude-opus-5-5"),
                              ("analyst", "claude-opus-5-5")}
        chat = usage[("chat", "claude-opus-5-5")]
        assert (chat.records, chat.model_calls, chat.input_tokens, chat.output_tokens,
                chat.cache_read_input_tokens, chat.cost_micros) == (1, 2, 1_000, 200, 9_000, 40_000)
        read = usage[("analyst", "claude-opus-5-5")]
        assert (read.records, read.model_calls, read.input_tokens, read.cost_micros) == (1, 1, 3_000, 25_000)

    def test_the_window_leaves_older_rows_out_and_is_bounded(self, integration_db, caps):
        me = _user(1)
        AiSpend.create(user_id=me.user_id, cost_micros=50_000, settled=True, created_at=NOW - timedelta(days=8))
        AiSpend.create(user_id=me.user_id, cost_micros=7_000, settled=True, created_at=NOW)

        r = run(spend_report.report(7, now=NOW))

        assert [row.cost_micros for row in r.rows] == [7_000]
        assert r.since == NOW.date() - timedelta(days=6) and r.days == 7
        with pytest.raises(BadRequestError):
            run(spend_report.report(91, now=NOW))

    def test_the_same_spend_on_two_days_stays_two_rows(self, integration_db, caps):
        me = _user(1)
        AiSpend.create(user_id=me.user_id, cost_micros=11_000, settled=True, created_at=NOW - timedelta(days=1))
        AiSpend.create(user_id=me.user_id, cost_micros=7_000, settled=True, created_at=NOW)

        r = run(spend_report.report(7, now=NOW))

        assert sorted((row.day, row.feature, row.cost_micros) for row in r.rows) == [
            ((NOW - timedelta(days=1)).date(), "unlinked", 11_000), (NOW.date(), "unlinked", 7_000)]

    def test_an_empty_ledger_still_names_every_line(self, integration_db, caps):
        r = run(spend_report.report(30, now=NOW))

        assert [line.line for line in r.lines] == ["desk", "autopilot", "analyst"]
        assert r.rows == [] and r.usage == [] and r.teams == []
        assert r.month_start == date(NOW.year, NOW.month, 1)
