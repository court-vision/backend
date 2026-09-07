"""
Integration: migration 0020 against the real schema.

The unit layer decides *what* to write; here the partial unique index decides
what may be written twice. One counted auto attempt (applied /
applied_unverified / noop) per team per fantasy day, while rejected / failed
rows — and every manual row — may repeat. Also the repository functions the
editor service runs through `run_db`, and the new preference column defaults.
"""

import json
from datetime import date, datetime

import pytest
from peewee import IntegrityError

from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.teams import Team
from db.models.users import User
from services import lineup_editor_service as svc

pytestmark = [pytest.mark.integration]

DAY = date(2026, 10, 20)


@pytest.fixture
def user(integration_db):
    return User.create(email="lineup@courtvision.dev", clerk_user_id="user_lineup", created_at=datetime.utcnow())


@pytest.fixture
def team(user):
    info = {"provider": "espn", "league_id": 426893737, "team_name": "Lvl. 3 Goblins", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="426893737Lvl. 3 Goblins", league_info=json.dumps(info))


def _row(user, team, source, status, day=DAY):
    return RosterMove.create(user=user.user_id, team=team.team_id, nba_date=day, scoring_period_id=1,
                             source=source, status=status, moves=[{"player_id": 1, "from_slot_id": 12, "to_slot_id": 11}])


def test_one_counted_auto_attempt_per_team_per_day(user, team):
    _row(user, team, "auto", "failed")           # a refused attempt does not count
    _row(user, team, "auto", "rejected")
    _row(user, team, "auto", "applied")          # the one that counts
    with pytest.raises(IntegrityError):
        _row(user, team, "auto", "noop")
    with pytest.raises(IntegrityError):
        _row(user, team, "auto", "applied_unverified")
    _row(user, team, "auto", "applied", day=date(2026, 10, 21))   # next day is a new slate
    _row(user, team, "manual", "applied")        # manual writes are never deduped
    _row(user, team, "manual", "applied")
    assert RosterMove.select().count() == 6


def test_repository_functions_round_trip(user, team):
    assert svc._auto_counted_today(team.team_id, DAY) is False
    audit_id = svc._audit_insert(user.user_id, team.team_id, DAY, 12, "auto",
                                 [{"player_id": 1, "from_slot_id": 12, "to_slot_id": 11, "role": "start", "note": None}],
                                 "21:12:v1:abc")
    row = RosterMove.get_by_id(audit_id)
    assert row.status == "failed" and row.error == "in_flight" and row.idempotency_key == "21:12:v1:abc"
    assert row.moves[0]["role"] == "start"
    assert svc._auto_counted_today(team.team_id, DAY) is False   # in flight does not count

    svc._audit_update(audit_id, "applied", provider_status=200)
    row = RosterMove.get_by_id(audit_id)
    assert row.status == "applied" and row.provider_status == 200 and row.error is None
    assert svc._auto_counted_today(team.team_id, DAY) is True

    svc._audit_noop(user.user_id, team.team_id, DAY, 12)          # already counted: silently no second row
    assert RosterMove.select().where(RosterMove.team == team.team_id).count() == 1


def test_noop_is_recorded_once(user, team):
    svc._audit_noop(user.user_id, team.team_id, DAY, 12)
    svc._audit_noop(user.user_id, team.team_id, DAY, 12)
    rows = list(RosterMove.select().where(RosterMove.team == team.team_id))
    assert len(rows) == 1 and rows[0].status == "noop" and rows[0].moves == []


def test_deleting_the_team_removes_its_audit_rows(user, team):
    _row(user, team, "manual", "applied")
    team.delete_instance()
    assert RosterMove.select().count() == 0


def test_auto_lineup_preference_defaults(user, team):
    prefs = NotificationPreference.create(user=user.user_id)
    assert NotificationPreference.get_by_id(prefs.id).auto_lineup_enabled is False
    override = NotificationTeamPreference.create(user=user.user_id, team_id=team.team_id)
    assert NotificationTeamPreference.get_by_id(override.id).auto_lineup_enabled is None
    override.auto_lineup_enabled = True
    override.save()
    assert NotificationTeamPreference.get_by_id(override.id).auto_lineup_enabled is True
