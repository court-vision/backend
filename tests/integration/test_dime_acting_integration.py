"""
Integration: Dime acting on its own against the real schema (migration 0044).

The levels on usr.autopilot (`ai_rows` and the check that keeps it to the four rows that
can be at AI, the autopilot switch and what it keeps to put back) and the writer that sets
them with lineup's levels below AI in the team's notification override, in one go;
usr.ai_settings (the account's AI features); usr.autopilot_actions (its checks, one action
per proposal, what the caps and auto-pause count, the reads the desk makes); a shadow
proposal promoted to a live chip once and only before its day starts; the team pausing
itself once; a player's recent adds (a Court Vision write or the ledger); the `autopilot`
source on usr.roster_moves outside the auto-lineup once-a-day index; `made_by_autopilot` on
usr.scheduled_pickups; and the rollback.
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
from db.models.autopilot import AiDecision, AiSettings, Autopilot, AutopilotAction
from db.models.move_outcomes import MoveOutcome
from db.models.notifications import NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from db.models.users import User
from services.ai import acting, ai_settings, checkins, conversations, levels

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
    fields = dict(team_id=team.team_id, user_id=team.user_id_id, capability="daily_actions", kind="transaction",
                  nba_date=DAY, adds=1, outcome="sent", message="Sent by Dime: Added Kawhi Leonard")
    fields.update(kw)
    return run(acting._record(**fields))


def _apply(team, **change):
    return run(levels.apply(team.team_id, team.user_id_id, levels.Change(**change), _now()))


def _lineup(team):
    row = NotificationTeamPreference.get_or_none((NotificationTeamPreference.user == team.user_id_id)
                                                 & (NotificationTeamPreference.team_id == team.team_id))
    return None if row is None else (row.lineup_alerts_enabled, row.auto_lineup_enabled)


class TestTheLevels:
    def test_a_team_has_nothing_at_ai_until_told_and_only_four_rows_can_be(self, team):
        Autopilot.create(team_id=team.team_id, user_id=team.user_id_id)
        row = Autopilot.get(Autopilot.team_id == team.team_id)
        assert (row.ai_rows, row.autopilot, row.restore) == ([], False, None)
        for bad in (["checkins"], ["lineup", "trades"]):
            with pytest.raises(IntegrityError), db.atomic():
                Autopilot.update(ai_rows=bad).where(Autopilot.team_id == team.team_id).execute()

    def test_a_row_at_ai_turns_check_ins_on_and_lineup_s_levels_are_the_team_s_override(self, team):
        made = _apply(team, levels={"daily_actions": "ai"})
        assert made.ai_rows == ["daily_actions"] and made.paused_at is None and _lineup(team) is None
        notify = _apply(team, levels={"lineup": "notify"})
        assert notify.ai_rows == ["daily_actions"] and _lineup(team) == (True, False)
        ai = _apply(team, levels={"lineup": "ai"})
        assert ai.ai_rows == ["lineup", "daily_actions"] and _lineup(team) == (True, True)
        off = _apply(team, levels={"lineup": "off", "daily_actions": "off"})
        assert off.ai_rows == [] and _lineup(team) == (False, False)
        assert off.paused_at is None                                    # check-ins are their own row
        assert NotificationTeamPreference.select().count() == 1         # one override, written in place

    def test_check_ins_alone_set_up_paused_and_resume_a_pause_dime_made(self, team):
        made = _apply(team, checkins=False)
        assert made.paused_at is not None and made.paused_by == "user" and made.ai_rows == []
        on = _apply(team, checkins=True)
        assert on.paused_at is None
        Autopilot.update(paused_at=_now(), paused_by="system", paused_reason="2 refused").where(
            Autopilot.team_id == team.team_id).execute()
        again = _apply(team, checkins=True)
        assert (again.paused_at, again.paused_by, again.paused_reason) == (None, None, None)
        # step 3's switch is the Check-ins row
        assert run(checkins._set_autopilot(team.team_id, team.user_id_id, False, _now())).paused_by == "user"

    def test_the_switch_puts_everything_at_ai_and_going_off_puts_back_what_it_changed(self, team):
        NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, lineup_alerts_enabled=False,
                                          auto_lineup_enabled=None)
        _apply(team, checkins=False)
        run(ai_settings.update(team.user_id_id, memory=False))
        on = _apply(team, autopilot=True)
        assert on.autopilot is True and on.paused_at is None and on.ai_rows == []
        assert on.restore == {"lineup": {"lineup_alerts_enabled": False, "auto_lineup_enabled": None},
                              "checkins_on": False}
        assert _lineup(team) == (True, True)
        assert run(ai_settings.get(team.user_id_id)) == ai_settings.AiSettings(memory=True, draft_rooms=True)
        assert _apply(team, autopilot=True).restore == on.restore          # on again: nothing new to keep
        off = _apply(team, autopilot=False)
        assert (off.autopilot, off.restore) == (False, None)
        assert _lineup(team) == (False, None) and off.paused_at is not None   # as they were
        assert run(ai_settings.get(team.user_id_id)).memory is True          # the features stay on

    def test_going_off_keeps_lineup_preferences_the_user_changed_while_it_was_on(self, team):
        NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, lineup_alerts_enabled=False)
        _apply(team, autopilot=True)
        NotificationTeamPreference.update(auto_lineup_enabled=False).execute()     # in Notifications
        _apply(team, autopilot=False)
        assert _lineup(team) == (True, False)

    def test_going_off_keeps_what_the_user_put_at_ai_themselves(self, team):
        _apply(team, levels={"lineup": "ai"})
        _apply(team, autopilot=True)
        off = _apply(team, autopilot=False)
        assert off.ai_rows == ["lineup"] and _lineup(team) == (True, True) and off.paused_at is None

    def test_changing_a_row_while_on_turns_the_switch_off_and_keeps_the_rest_at_ai(self, team):
        _apply(team, autopilot=True)
        row = _apply(team, levels={"lanes": "act"})
        assert (row.autopilot, row.restore) == (False, None)
        assert row.ai_rows == ["lineup", "daily_actions"] and _lineup(team) == (True, True)

    def test_a_double_first_switch_writes_one_row(self, team):
        _apply(team, levels={"lanes": "ai"})
        _apply(team, levels={"lanes": "ai"})
        assert Autopilot.select().where(Autopilot.team_id == team.team_id).count() == 1


class TestTheFeatures:
    def test_both_on_with_no_row_and_a_change_keeps_the_other(self, team):
        assert run(ai_settings.get(team.user_id_id)) == ai_settings.AiSettings()
        assert AiSettings.select().count() == 0
        assert run(ai_settings.update(team.user_id_id, memory=False)) == ai_settings.AiSettings(memory=False)
        got = run(ai_settings.update(team.user_id_id, draft_rooms=False))
        assert got == ai_settings.AiSettings(memory=False, draft_rooms=False)
        assert run(ai_settings.update(team.user_id_id)) == got                 # nothing to change
        User.delete().where(User.user_id == team.user_id_id).execute()
        assert AiSettings.select().count() == 0                                # they go with the account


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

    def test_dime_s_moves_list_the_call_and_the_check_in_newest_first(self, team):
        checkin_id, conv = _checkin(team)
        p = _proposal(team, conv)
        d = _decision(team, checkin_id, p)
        _action(team, checkin_id=checkin_id, decision_id=d.id, proposal_id=p.id)
        _action(team, outcome="blocked", rule="cap_day", message="Dime has made its 2 add(s)", capability="lanes",
                kind="lane", adds=0)
        listed = run(acting.list_actions(team.user_id_id, team.team_id))
        assert [a["outcome"] for a in listed] == ["blocked", "sent"]
        held, sent = listed
        assert (held["row"], held["rule"], held["summary"], held["conversation_id"]) == ("lanes", "cap_day", None, None)
        assert (sent["summary"], sent["conversation_id"], sent["checkin_kind"], sent["proposal_id"]) == \
            ("Add Kawhi", conv.id, "morning", p.id)
        assert sent["at"].tzinfo is not None
        assert run(acting.list_actions(team.user_id_id, team.team_id, limit=1))[0]["outcome"] == "blocked"
        assert run(acting.list_actions(team.user_id_id + 1)) == []                 # only the user's own

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
        assert not {"ai_rows", "autopilot", "restore"} & _columns("autopilot")
        assert "made_by_autopilot" not in _columns("scheduled_pickups")
        assert not _columns("autopilot_actions") and not _columns("ai_settings")
        assert RosterMove.get().source == "manual"                     # kept, as the old CHECK allows
    finally:
        db.execute_sql(forward)
    assert {"ai_rows", "autopilot", "restore"} <= _columns("autopilot")
    assert {"memory", "draft_rooms"} <= _columns("ai_settings")
    assert "outcome" in _columns("autopilot_actions") and "made_by_autopilot" in _columns("scheduled_pickups")
    db.execute_sql(forward)                                            # idempotent
