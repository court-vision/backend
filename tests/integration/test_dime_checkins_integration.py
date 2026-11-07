"""
Integration: Dime's check-ins in shadow against the real schema (migration 0042).

The claim that runs each scheduled check-in once per team, kind and day (a
manual run is outside it), the sweep that closes check-ins whose process died,
the switch (paused, resumed with a new `active_since`), the decisions a
check-in logs (only its own team, only its own proposals) and the verdicts the
user sets on them, the check-in conversations kept out of the AI desk's list,
and the check-ins' spend line, apart from the desk's caps.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from core.errors import AppError, ConflictError
from core.settings import settings
from db.models.ai_conversations import AiConversation, AiProposal, AiSpend
from db.models.autopilot import AiCheckin, AiDecision, Autopilot
from db.models.teams import Team
from db.models.users import User
from services.ai import checkins, conversations, decisions, proposals, spend

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 10)


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


def _user(n):
    return User.create(email=f"dime{n}@courtvision.dev", clerk_user_id=f"user_dime_{n}", created_at=_now())


def _team(user):
    info = {"provider": "espn", "league_id": 1234, "team_name": "Mine", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="1234Mine", league_info=json.dumps(info))


def _claim(team, user, kind="morning", manual=False, state="running", started=None):
    return run(checkins._claim(team.team_id, user.user_id, kind, DAY, _now(), manual=manual,
                               now=started or _now(), state=state))


def _checkin(team, user):
    conv = run(conversations.create(user.user_id, team.team_id, kind=conversations.CHECKIN, title="Morning"))
    checkin_id = _claim(team, user)
    run(checkins._attach(checkin_id, conv.id))
    return checkin_id, conv


def _proposal(user, team, conv, state="shadow"):
    return AiProposal.create(user_id=user.user_id, conversation_id=conv.id, team_id=team.team_id, kind="transaction",
                             day=1, scoring_period_id=20, nba_date=DAY, add_player_id=1, add_name="Herb Jones",
                             drop_player_id=2, drop_name="Kris Dunn", reason="Four games", summary="Add Herb Jones",
                             state=state, expires_at=_now() + timedelta(hours=8))


def _log(checkin_id, team, user, **kw):
    args = {"team_id": team.team_id, "area": "lineup", "automation": "auto_lineup", "call": "hold",
            "summary": "Keep auto-lineup's fill", "reason": "Every starter plays", **kw}
    return run(decisions.log_decision(user.user_id, checkin_id, decisions.LogDecisionInput(**args)))


class TestClaims:
    def test_a_scheduled_slot_runs_once_and_a_manual_run_is_outside_the_rule(self, integration_db):
        me = _user(1)
        team = _team(me)
        first = _claim(team, me)
        assert first is not None
        assert _claim(team, me) is None
        assert _claim(team, me, state="missed") is None
        run(checkins._settle(first, "done", None, None, 0, _now()))
        second = _claim(team, me, kind="pre_lock")
        assert second is not None
        run(checkins._settle(second, "done", None, None, 0, _now()))
        manual = _claim(team, me, manual=True)
        assert manual is not None
        run(checkins._settle(manual, "done", None, None, 0, _now()))
        assert _claim(team, me, manual=True) is not None
        assert run(checkins._taken(team.team_id, [DAY])) == {("morning", DAY), ("pre_lock", DAY)}
        assert run(checkins._busy(team.team_id)) is True
        assert run(checkins._ran_since(team.team_id, _now() - timedelta(days=1))) == 4

    def test_one_runs_per_team_at_a_time_whatever_a_tick_read_before(self, integration_db):
        me, them = _user(1), _user(2)
        team, other = _team(me), _team(them)
        running = _claim(team, me)
        assert running is not None
        # Another slot, a manual run: refused at the insert while one runs (two overlapping ticks,
        # or Run now during a tick, can't both start one)
        assert _claim(team, me, kind="pre_lock") is None
        assert _claim(team, me, manual=True) is None
        # A missed or skipped one isn't running, so it still lands; another team runs alongside
        assert _claim(team, me, kind="night", state="missed") is not None
        assert _claim(other, them) is not None
        run(checkins._settle(running, "failed", "AI_TIMEOUT", None, 0, _now()))
        assert _claim(team, me, kind="pre_lock") is not None

    def test_missed_and_skipped_ones_close_at_once_and_don_t_count_toward_the_cap(self, integration_db):
        me = _user(1)
        team = _team(me)
        missed = _claim(team, me, state="missed")
        row = AiCheckin.get_by_id(missed)
        assert row.state == "missed" and row.finished_at is not None
        _claim(team, me, kind="night", state="skipped")
        assert run(checkins._ran_since(team.team_id, _now() - timedelta(days=1))) == 0
        assert run(checkins._busy(team.team_id)) is False

    def test_a_check_in_whose_process_died_is_swept_and_its_late_turn_still_settles(self, integration_db):
        me = _user(1)
        team = _team(me)
        them = _user(2)
        other = _team(them)
        old = _claim(team, me, started=_now() - timedelta(hours=1))
        fresh = _claim(other, them, kind="pre_lock")
        assert run(checkins._sweep(_now() - timedelta(minutes=20), _now())) == 1
        assert AiCheckin.get_by_id(old).state == "failed" and AiCheckin.get_by_id(old).reason == "stale"
        assert AiCheckin.get_by_id(fresh).state == "running"
        run(checkins._settle(old, "done", None, None, 41_000, _now()))
        row = AiCheckin.get_by_id(old)
        assert (row.state, row.reason, row.cost_micros) == ("done", None, 41_000)


class TestSwitch:
    def test_on_pause_and_resume(self, integration_db):
        me = _user(1)
        team = _team(me)
        on = run(checkins._set_autopilot(team.team_id, me.user_id, True, _now()))
        assert on.paused_at is None
        assert [r.team_id_id for r in run(checkins._live())] == [team.team_id]

        paused = run(checkins._set_autopilot(team.team_id, me.user_id, False, _now()))
        assert paused.paused_at is not None and paused.paused_by == "user"
        assert run(checkins._live()) == []

        later = _now() + timedelta(minutes=5)
        resumed = run(checkins._set_autopilot(team.team_id, me.user_id, True, later))
        assert resumed.paused_at is None and resumed.paused_by is None
        assert resumed.active_since == later  # what fell due while paused is not missed

    def test_a_team_that_changed_hands_is_not_live(self, integration_db):
        me, other = _user(1), _user(2)
        team = _team(me)
        run(checkins._set_autopilot(team.team_id, me.user_id, True, _now()))
        Team.update(user_id=other.user_id).where(Team.team_id == team.team_id).execute()
        assert run(checkins._live()) == []

    def test_the_teams_view_lists_only_the_allowlisted_teams_the_user_owns(self, integration_db, monkeypatch):
        me, other = _user(1), _user(2)
        mine, theirs = _team(me), _team(other)
        monkeypatch.setattr(settings, "ai_agent_user_ids", [me.user_id])
        monkeypatch.setattr(settings, "autopilot_team_ids", [mine.team_id, theirs.team_id])
        pairs = run(checkins._autopilot_teams(me.user_id, settings.autopilot_team_ids))
        assert pairs == [(mine.team_id, None)]


class TestDecisions:
    def test_a_check_in_logs_its_calls_and_the_user_marks_them(self, integration_db):
        me = _user(1)
        team = _team(me)
        checkin_id, conv = _checkin(team, me)
        shadow = _proposal(me, team, conv)

        hold = _log(checkin_id, team, me)
        change = _log(checkin_id, team, me, area="adds_drops", automation="none", call="change",
                      summary="Add Herb Jones for Kris Dunn", reason="Four games to two", proposal_id=shadow.id)
        assert change["move"]["proposal_id"] == shadow.id and change["move"]["state"] == "shadow"

        run(decisions.set_verdict(hold["decision_id"], me.user_id, "agree"))
        run(decisions.set_verdict(change["decision_id"], me.user_id, "disagree"))
        stored = AiDecision.get_by_id(change["decision_id"])
        assert stored.verdict == "disagree" and stored.verdict_at is not None
        run(decisions.set_verdict(change["decision_id"], me.user_id, None))
        assert AiDecision.get_by_id(change["decision_id"]).verdict_at is None

        read = run(decisions.get_decisions(me.user_id, decisions.GetDecisionsInput(team_id=team.team_id)))
        assert read["summary"] == {"decisions": 2, "hold": 1, "change": 1, "agree": 1, "disagree": 0, "unrated": 1}
        newest = read["decisions"][0]
        assert newest["call"] == "change" and newest["move"]["add"]["name"] == "Herb Jones"
        assert newest["checkin"] == {"kind": "morning", "date": DAY.isoformat(), "manual": False}

        tallies = run(checkins.inbox(me.user_id, team.team_id))
        assert tallies[0]["decisions"] == 2 and tallies[0]["changes"] == 1 and tallies[0]["agree"] == 1
        assert tallies[0]["conversation_id"] == conv.id

    def test_it_refuses_another_conversation_s_proposal_and_a_finished_check_in(self, integration_db):
        me = _user(1)
        team = _team(me)
        checkin_id, _ = _checkin(team, me)
        elsewhere = run(conversations.create(me.user_id, team.team_id))
        stray = _proposal(me, team, elsewhere, state="proposed")
        with pytest.raises(AppError, match="wasn't made in this check-in"):
            _log(checkin_id, team, me, call="change", proposal_id=stray.id)
        run(checkins._settle(checkin_id, "done", None, None, 0, _now()))
        with pytest.raises(AppError, match="no longer running"):
            _log(checkin_id, team, me)

    def test_another_user_can_t_mark_a_decision(self, integration_db):
        me, other = _user(1), _user(2)
        team = _team(me)
        checkin_id, _ = _checkin(team, me)
        logged = _log(checkin_id, team, me)
        with pytest.raises(AppError) as err:
            run(decisions.set_verdict(logged["decision_id"], other.user_id, "agree"))
        assert err.value.error_code == "AI_DECISION_NOT_FOUND"

    def test_the_conversation_s_blocks_show_the_verdict_as_it_stands(self, integration_db):
        me = _user(1)
        team = _team(me)
        checkin_id, conv = _checkin(team, me)
        logged = _log(checkin_id, team, me)
        part = {"kind": "block", "block": {"type": "decision", "id": "d1", **logged[decisions.DECISION_KEY]}}
        run(decisions.set_verdict(logged["decision_id"], me.user_id, "agree"))
        out = run(decisions.refresh_history([[part]], conv.id, me.user_id))
        assert out[0][0]["block"]["verdict"] == "agree"
        assert run(checkins.of_conversation(conv.id, me.user_id))["checkin_id"] == checkin_id


class TestShadow:
    def test_a_shadow_proposal_is_stored_and_never_dismissed_or_marked_sent(self, integration_db):
        me = _user(1)
        team = _team(me)
        _, conv = _checkin(team, me)
        shadow = _proposal(me, team, conv)
        with pytest.raises(ConflictError) as err:
            run(proposals.dismiss(shadow.id, me.user_id))
        assert err.value.error_code == "AI_PROPOSAL_SHADOW"
        assert AiProposal.get_by_id(shadow.id).state == "shadow"

    def test_check_in_conversations_stay_out_of_the_desk_s_list(self, integration_db):
        me = _user(1)
        team = _team(me)
        chat = run(conversations.create(me.user_id, team.team_id))
        _checkin(team, me)
        assert [item["conversation"].id for item in run(conversations.list_for(me.user_id))] == [chat.id]
        assert AiConversation.select().where(AiConversation.kind == "checkin").count() == 1


class TestSpendLine:
    def test_check_ins_spend_on_their_own_line(self, integration_db, monkeypatch):
        me = _user(1)
        monkeypatch.setattr(settings, "ai_agent_user_ids", [me.user_id])
        monkeypatch.setattr(settings, "ai_monthly_budget_usd", 10.0)
        monkeypatch.setattr(settings, "ai_agent_turn_max_usd", 1.0)
        monkeypatch.setattr(settings, "autopilot_monthly_budget_usd", 2.5)

        first = run(spend.reserve_autopilot(me.user_id))
        second = run(spend.reserve_autopilot(me.user_id))
        assert first and second and AiSpend.get_by_id(first).line == "autopilot"
        # A third ceiling would pass 2.5: refused, not raised
        assert run(spend.reserve_autopilot(me.user_id)) is None
        # The desk's caps don't see it
        assert run(spend.month_spend(me.user_id)).user == 0
        assert run(spend.autopilot_month_spend()) == (2_000_000, 2_500_000)

        run(spend.settle(first, 40_000))
        assert run(spend.reserve_autopilot(me.user_id)) is not None
        desk = run(spend.reserve(me.user_id))
        assert AiSpend.get_by_id(desk).line == "desk"
