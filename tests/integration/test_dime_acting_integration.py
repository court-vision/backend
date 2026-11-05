"""
Integration: Dime acting on its own against the real schema (migration 0044).

The modes on usr.autopilot (the team's, each kind's own, the check that keeps them to
shadow / suggest / act) and the settings write that sets them; usr.autopilot_actions
(its checks, one action per proposal, what the caps and auto-pause count, the reads the
desk makes); a shadow proposal promoted to a live chip once and only before its day
starts; the team pausing itself once; a player's recent adds (a Court Vision write or the
ledger); the `autopilot` source on usr.roster_moves outside the auto-lineup once-a-day
index; `made_by_autopilot` on usr.scheduled_pickups; and the rollback.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.ai_conversations import AiProposal
from db.models.autopilot import AiDecision, Autopilot, AutopilotAction
from db.models.move_outcomes import MoveOutcome
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from db.models.users import User
from services.ai import acting, checkins, conversations

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 10)


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def team(integration_db):
    user = User.create(email="act@courtvision.dev", clerk_user_id="user_act", created_at=_now())
    info = {"provider": "espn", "league_id": 1234, "team_name": "Mine", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="1234Mine", league_info=json.dumps(info))


def _checkin(team):
    conv = run(conversations.create(team.user_id_id, team.team_id, kind=conversations.CHECKIN, title="Morning"))
    checkin_id = run(checkins._claim(team.team_id, team.user_id_id, "morning", DAY, _now(), manual=False,
                                         now=_now()))
    run(checkins._attach(checkin_id, conv.id))
    return checkin_id, conv


def _proposal(team, conv, **kw):
    fields = dict(user_id=team.user_id_id, conversation_id=conv.id, team_id=team.team_id, kind="transaction", day=1,
                  scoring_period_id=20, nba_date=DAY, add_player_id=6450, add_name="Kawhi Leonard",
                  drop_player_id=4594268, drop_name="Anthony Edwards", reason="4 games left",
                  summary="Add Kawhi Leonard, drop Anthony Edwards, today. Logged in shadow, not sent.",
                  state="shadow", expires_at=_now() + timedelta(hours=8))
    fields.update(kw)
    return AiProposal.create(**fields)


def _decision(team, checkin_id, proposal):
    return AiDecision.create(checkin_id=checkin_id, user_id=team.user_id_id, team_id=team.team_id, area="adds_drops",
                             automation="none", call="change", summary="Add Kawhi", reason="4 games left",
                             proposal_id=proposal.id)


def _action(team, **kw):
    fields = dict(team_id=team.team_id, user_id=team.user_id_id, capability="adds_drops", kind="transaction",
                  nba_date=DAY, adds=1, outcome="sent", message="Sent by Dime: Added Kawhi Leonard")
    fields.update(kw)
    return run(acting._record(**fields))


class TestTheModes:
    def test_a_team_is_shadow_until_told_and_each_mode_is_one_of_three(self, team):
        Autopilot.create(team_id=team.team_id, user_id=team.user_id_id)
        row = Autopilot.get(Autopilot.team_id == team.team_id)
        assert (row.mode, row.lineup_mode, row.streaming_mode, row.adds_drops_mode) == ("shadow", None, None, None)
        for bad in (dict(mode="autopilot"), dict(lineup_mode="always"), dict(adds_drops_mode="")):
            with pytest.raises(IntegrityError), db.atomic():
                Autopilot.update(**bad).where(Autopilot.team_id == team.team_id).execute()

    def test_the_settings_write_sets_the_modes_without_touching_the_switch(self, team):
        now = _now()
        made = run(checkins._set_autopilot(team.team_id, team.user_id_id, None, now,
                                           {"mode": "suggest", "adds_drops_mode": "act"}))
        assert made.paused_at is not None                      # never switched on: set up paused
        assert (made.mode, made.adds_drops_mode) == ("suggest", "act")
        on = run(checkins._set_autopilot(team.team_id, team.user_id_id, True, now, {"adds_drops_mode": None}))
        assert on.paused_at is None and (on.mode, on.adds_drops_mode) == ("suggest", None)
        row = Autopilot.get(Autopilot.team_id == team.team_id)
        assert (row.mode, row.adds_drops_mode, row.paused_at) == ("suggest", None, None)


class TestTheActionsLog:
    def test_its_checks_and_one_action_per_proposal(self, team):
        checkin_id, conv = _checkin(team)
        p = _proposal(team, conv)
        d = _decision(team, checkin_id, p)
        first = _action(team, checkin_id=checkin_id, decision_id=d.id, proposal_id=p.id)
        assert first is not None
        assert _action(team, proposal_id=p.id) is None                 # a second decision citing it
        for bad in (dict(outcome="pending"), dict(capability="trades"), dict(message="")):
            with pytest.raises(IntegrityError), db.atomic():
                AutopilotAction.create(**{**dict(team_id=team.team_id, user_id=team.user_id_id, capability="lineup",
                                                 kind="lineup", nba_date=DAY, outcome="sent", message="x"), **bad})
        # The proposal, decision and check-in may go; the log stays
        AiProposal.delete().where(AiProposal.id == p.id).execute()
        assert AutopilotAction.get_by_id(first.id).proposal_id_id is None

    def test_what_the_caps_auto_pause_and_the_desk_read(self, team):
        checkin_id, conv = _checkin(team)
        d = _decision(team, checkin_id, _proposal(team, conv))
        _action(team, checkin_id=checkin_id, decision_id=d.id)
        _action(team, nba_date=DAY + timedelta(days=1))
        _action(team, nba_date=DAY, adds=0, kind="lineup", capability="lineup")
        _action(team, outcome="blocked", rule="cap_day", message="Dime has made its 2 add(s)")
        _action(team, outcome="refused", rule="espn_rejected", message="ESPN refused it")
        assert run(acting._adds_sent(team.team_id, DAY, DAY)) == 1
        assert run(acting._adds_sent(team.team_id, DAY, DAY + timedelta(days=6))) == 2
        since = _now() - timedelta(hours=1)
        assert run(acting._count_since(team.team_id, since, "blocked", "cap_day")) == 1
        assert run(acting._count_since(team.team_id, since, "blocked", "protected")) == 0
        assert run(acting._count_since(team.team_id, since, "refused")) == 1
        assert run(acting._count_since(team.team_id, _now() + timedelta(minutes=1), "refused")) == 0
        got = run(acting.actions_of([d.id]))
        assert got[d.id]["outcome"] == "sent" and got[d.id]["at"].endswith("+00:00")
        assert run(acting.sent_counts([checkin_id])) == {checkin_id: 1}
        recent = run(acting._recent(team.team_id, 3))
        assert [a.outcome for a in recent] == ["refused", "blocked", "sent"]

    def test_a_team_s_actions_go_with_it(self, team):
        _action(team)
        Team.delete().where(Team.team_id == team.team_id).execute()
        assert AutopilotAction.select().count() == 0


class TestTheWrites:
    def test_a_shadow_proposal_is_promoted_once_and_only_before_its_day_starts(self, team):
        _, conv = _checkin(team)
        p = _proposal(team, conv)
        late = _proposal(team, conv, expires_at=_now() - timedelta(minutes=1))
        assert run(acting._promote(p.id, "Add Kawhi Leonard. Goes to ESPN when you send it.", _now())) is True
        got = AiProposal.get_by_id(p.id)
        assert (got.state, got.summary) == ("proposed", "Add Kawhi Leonard. Goes to ESPN when you send it.")
        assert run(acting._promote(p.id, "again", _now())) is False
        assert run(acting._promote(late.id, "late", _now())) is False and AiProposal.get_by_id(late.id).state == "shadow"

    def test_the_team_pauses_itself_once(self, team):
        Autopilot.create(team_id=team.team_id, user_id=team.user_id_id, paused_at=None)
        assert run(acting._pause(team.team_id, "2 of its moves were refused today", _now())) is True
        assert run(acting._pause(team.team_id, "again", _now())) is False
        row = Autopilot.get(Autopilot.team_id == team.team_id)
        assert (row.paused_by, row.paused_reason) == ("system", "2 of its moves were refused today")

    def test_a_recent_add_is_a_court_vision_write_or_a_ledger_move(self, team):
        since, since_day = _now() - timedelta(hours=48), DAY - timedelta(days=2)
        assert run(acting._recent_adds(team.team_id, 6450, since, since_day)) is False
        RosterMove.create(user=team.user_id_id, team=team.team_id, nba_date=DAY, source="manual", kind="transaction",
                          status="rejected", moves=[{"action": "add", "player_id": 6450}], created_at=_now())
        assert run(acting._recent_adds(team.team_id, 6450, since, since_day)) is False        # it didn't go through
        RosterMove.create(user=team.user_id_id, team=team.team_id, nba_date=DAY, source="autopilot", kind="transaction",
                          status="applied", moves=[{"action": "add", "player_id": 6450},
                                                   {"action": "drop", "player_id": 1}], created_at=_now())
        assert run(acting._recent_adds(team.team_id, 6450, since, since_day)) is True
        assert run(acting._recent_adds(team.team_id, 1, since, since_day)) is False           # a drop isn't an add
        MoveOutcome.create(provider="espn", provider_league_id="1234", season=2027, provider_team_id=1,
                           team_name="Mine", scoring_period_id=20, nba_date=DAY - timedelta(days=1),
                           add_player_id=99, paired=False, source="outside", team_id=team.team_id,
                           format="points", windows={})
        assert run(acting._recent_adds(team.team_id, 99, since, since_day)) is True
        assert run(acting._recent_adds(team.team_id, 99, since, DAY)) is False

    def test_dime_s_writes_are_their_own_source_outside_auto_lineup_s_once_a_day(self, team):
        for _ in range(2):
            RosterMove.create(user=team.user_id_id, team=team.team_id, nba_date=DAY, source="autopilot",
                              kind="lineup", status="applied", moves=[])
        with pytest.raises(IntegrityError), db.atomic():
            RosterMove.create(user=team.user_id_id, team=team.team_id, nba_date=DAY, source="robot",
                              kind="lineup", status="applied", moves=[])

    def test_a_pickup_says_whether_dime_scheduled_it(self, team):
        fields = dict(user=team.user_id_id, team=team.team_id, scoring_period_id=21, nba_date=DAY + timedelta(days=1),
                      add_player_id=6450, add_name="Kawhi Leonard", drop_player_id=4594268,
                      drop_name="Anthony Edwards", not_before_at=_now() + timedelta(hours=10))
        mine = ScheduledPickup.create(**fields)
        dimes = ScheduledPickup.create(**{**fields, "add_player_id": 99, "made_by_autopilot": True})
        assert ScheduledPickup.get_by_id(mine.id).made_by_autopilot is False
        assert ScheduledPickup.get_by_id(dimes.id).made_by_autopilot is True


def _columns(table):
    return {r[0] for r in db.execute_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'usr' AND table_name = %s",
        (table,)).fetchall()}


def test_0044_rollback_applies_and_the_forward_file_restores_it(team):
    forward = (Path(MIGRATIONS_DIR) / "0044__dime_acting.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0044__dime_acting.rollback.sql").read_text()
    RosterMove.create(user=team.user_id_id, team=team.team_id, nba_date=DAY, source="autopilot", kind="lineup",
                      status="applied", moves=[])
    try:
        db.execute_sql(rollback)
        assert "mode" not in _columns("autopilot") and "made_by_autopilot" not in _columns("scheduled_pickups")
        assert not _columns("autopilot_actions")
        assert RosterMove.get().source == "manual"                     # kept, as the old CHECK allows
    finally:
        db.execute_sql(forward)
    assert {"mode", "lineup_mode", "streaming_mode", "adds_drops_mode"} <= _columns("autopilot")
    assert "outcome" in _columns("autopilot_actions") and "made_by_autopilot" in _columns("scheduled_pickups")
    db.execute_sql(forward)                                            # idempotent
