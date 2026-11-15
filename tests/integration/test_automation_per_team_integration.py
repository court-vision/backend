"""
Integration: migration 0049 (AI and automation per team) against the real schema.

The forward file runs over seeded preferences: every team of an account with auto-lineup on or Late
scratches above off takes the account's values where it has none of its own (its own values and its
notification fields stay), a team on autopilot that remembered auto-lineup as the account's now
remembers the account's value, the account's values go back to off, and an account at the defaults
is left alone. What each team does (lineup and late scratches below AI, as data-platform and the
late-scratch pass read them) is the same before and after. Plus the rollback.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.autopilot import Autopilot
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.teams import Team
from db.models.users import User
from services.ai import levels

pytestmark = [pytest.mark.integration]

FORWARD = (Path(MIGRATIONS_DIR) / "0049__automation_per_team.sql").read_text()
ROLLBACK = (Path(MIGRATIONS_DIR) / "0049__automation_per_team.rollback.sql").read_text()


def _user(n):
    return User.create(email=f"auto{n}@courtvision.dev", clerk_user_id=f"user_auto_{n}", created_at=datetime.utcnow())


def _team(user, name):
    info = {"provider": "espn", "league_id": 4321, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"4321{name}", league_info=json.dumps(info))


def _override(user, team, **fields):
    return NotificationTeamPreference.create(user=user.user_id, team_id=team.team_id, **fields)


def _team_pref(user, team):
    return NotificationTeamPreference.get_or_none((NotificationTeamPreference.user == user.user_id)
                                                  & (NotificationTeamPreference.team_id == team.team_id))


def _levels(user, teams):
    """Each team's lineup and late-scratch level below AI, as the pipelines read them."""
    prefs = NotificationPreference.get_or_none(NotificationPreference.user == user.user_id)
    return {t.team_id: (levels.lineup_below_ai(prefs, _team_pref(user, t)),
                        levels.late_scratch_below_ai(prefs, _team_pref(user, t))) for t in teams}


@pytest.fixture
def seeded(integration_db):
    db.execute_sql("DELETE FROM usr.account_automation_0049")      # the chain's own run left it empty
    me, plain = _user(1), _user(2)
    NotificationPreference.create(user=me.user_id, lineup_alerts_enabled=True, auto_lineup_enabled=True,
                                  late_scratch_level="notify", alert_minutes_before=60)
    NotificationPreference.create(user=plain.user_id, lineup_alerts_enabled=True)
    inherits, own, partial = _team(me, "Inherits"), _team(me, "Own"), _team(me, "Partial")
    _override(me, own, lineup_alerts_enabled=False, auto_lineup_enabled=False, late_scratch_level="act")
    _override(me, partial, alert_minutes_before=30, email="team@courtvision.dev")
    Autopilot.create(team_id=inherits.team_id, user_id=me.user_id, autopilot=True, ai_rows=[],
                     restore={"lineup": {"lineup_alerts_enabled": None, "auto_lineup_enabled": None},
                              "checkins_on": False})
    theirs = _team(plain, "Plain")
    yield me, plain, (inherits, own, partial), theirs
    db.execute_sql("DELETE FROM usr.account_automation_0049")


def test_0049_is_applied_by_the_chain(integration_db):
    ids = {r[0] for r in db.execute_sql("SELECT migration_id FROM public._yoyo_migration").fetchall()}
    assert "0049__automation_per_team" in ids


def test_each_team_keeps_what_it_does_and_the_account_goes_back_to_off(seeded):
    me, plain, (inherits, own, partial), theirs = seeded
    before = _levels(me, [inherits, own, partial])
    assert before == {inherits.team_id: ("act", "notify"), own.team_id: ("off", "act"),
                      partial.team_id: ("act", "notify")}

    db.execute_sql(FORWARD)

    assert _levels(me, [inherits, own, partial]) == before
    account = NotificationPreference.get(NotificationPreference.user == me.user_id)
    assert (account.auto_lineup_enabled, account.late_scratch_level) == (False, "off")
    assert account.lineup_alerts_enabled is True and account.alert_minutes_before == 60   # Notifications' own

    added = _team_pref(me, inherits)                                    # a row where it had none
    assert (added.auto_lineup_enabled, added.late_scratch_level) == (True, "notify")
    assert added.lineup_alerts_enabled is None and added.email is None  # notifications still the account's
    kept = _team_pref(me, own)                                          # its own values stay
    assert (kept.lineup_alerts_enabled, kept.auto_lineup_enabled, kept.late_scratch_level) == (False, False, "act")
    filled = _team_pref(me, partial)
    assert (filled.auto_lineup_enabled, filled.late_scratch_level) == (True, "notify")
    assert (filled.alert_minutes_before, filled.email) == (30, "team@courtvision.dev")

    restore = Autopilot.get(Autopilot.team_id == inherits.team_id).restore
    assert restore == {"lineup": {"lineup_alerts_enabled": None, "auto_lineup_enabled": True}, "checkins_on": False}

    # An account at the defaults is left as it was, and its teams get no row
    assert _team_pref(plain, theirs) is None
    assert NotificationPreference.get(NotificationPreference.user == plain.user_id).auto_lineup_enabled is False


def test_a_team_added_after_starts_with_nothing_acting(seeded):
    me, _, _, _ = seeded
    db.execute_sql(FORWARD)
    later = _team(me, "Later")
    assert _levels(me, [later]) == {later.team_id: ("notify", "off")}


def test_the_rollback_puts_the_account_back_and_the_forward_file_runs_again(seeded):
    me, _, (inherits, own, partial), _ = seeded
    before = _levels(me, [inherits, own, partial])
    db.execute_sql(FORWARD)
    db.execute_sql(ROLLBACK)
    account = NotificationPreference.get(NotificationPreference.user == me.user_id)
    assert (account.auto_lineup_enabled, account.late_scratch_level) == (True, "notify")
    assert _levels(me, [inherits, own, partial]) == before
    assert db.execute_sql("SELECT to_regclass('usr.account_automation_0049')").fetchone()[0] is None

    db.execute_sql(FORWARD)
    db.execute_sql(FORWARD)                                             # idempotent
    account = NotificationPreference.get(NotificationPreference.user == me.user_id)
    assert (account.auto_lineup_enabled, account.late_scratch_level) == (False, "off")
    assert _levels(me, [inherits, own, partial]) == before
