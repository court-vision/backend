"""
Integration: Daily actions at Act against the real schema (migration 0057,
services/daily_actions_job.py). What the pass makes of a list is tested in
tests/unit/test_daily_actions_job.py; here are the level it reads (written through
the settings table and the notification preferences), the pickups it keeps spots
for, and the audit source its writes are stored under.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.autopilot import Autopilot
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from db.models.users import User
from services import daily_actions_job as job
from services import lineup_editor_service as svc
from services.ai import autonomy, levels

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 24)


@pytest.fixture
def team(integration_db):
    user = User.create(email="daily@courtvision.dev", clerk_user_id="user_daily", created_at=datetime.utcnow())
    info = {"provider": "espn", "league_id": 552315826, "team_name": "Test", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="552315826Daily", league_info=json.dumps(info))


def level_of(team):
    return asyncio.run(job._level_of(team.team_id, team.user_id_id))


def test_the_level_defaults_to_off_and_the_override_wins(team):
    assert level_of(team) == ("off", False)
    NotificationPreference.create(user=team.user_id_id, email="daily@courtvision.dev")
    assert NotificationPreference.get(NotificationPreference.user == team.user_id_id).daily_actions_level == "off"
    NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, daily_actions_level="act")
    assert level_of(team) == ("act", False)


def test_the_level_is_written_through_the_settings_table_and_ai_leaves_it(team):
    now = datetime.now(timezone.utc)
    NotificationPreference.create(user=team.user_id_id, email="daily@courtvision.dev", lineup_alerts_enabled=True)
    levels.apply.__wrapped__(team.team_id, team.user_id_id, levels.Change(levels={"daily_actions": "act"}), now)
    override = NotificationTeamPreference.get(NotificationTeamPreference.team_id == team.team_id)
    assert override.daily_actions_level == "act"
    assert level_of(team) == ("act", False)

    # At AI the override keeps the user's level and the pass leaves the team to Dime
    row = levels.apply.__wrapped__(team.team_id, team.user_id_id, levels.Change(levels={"daily_actions": "ai"}), now)
    assert autonomy.at_ai(row, autonomy.DAILY_ACTIONS)
    assert NotificationTeamPreference.get(NotificationTeamPreference.team_id == team.team_id).daily_actions_level == "act"
    assert level_of(team) == ("act", True)

    levels.apply.__wrapped__(team.team_id, team.user_id_id, levels.Change(levels={"daily_actions": "off"}), now)
    assert level_of(team) == ("off", False)


def test_the_autopilot_switch_puts_daily_actions_at_ai(team):
    now = datetime.now(timezone.utc)
    NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, daily_actions_level="act")
    Autopilot.create(team_id=team.team_id, user_id=team.user_id_id, autopilot=True, created_at=now, updated_at=now,
                     active_since=now)
    assert level_of(team) == ("act", True)


def test_a_level_outside_the_check_is_refused(team):
    with pytest.raises(IntegrityError):
        NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, daily_actions_level="ai")


def test_the_pending_pickups_it_keeps_spots_for(team):
    now = datetime.utcnow()

    def pickup(add_id, name, day, *, drop=None, status="pending"):
        ScheduledPickup.create(user=team.user_id_id, team=team.team_id, add_player_id=add_id, add_name=name,
                               drop_player_id=drop, scoring_period_id=36, nba_date=day, not_before_at=now,
                               status=status)

    pickup(1, "Toumani Camara", DAY + timedelta(days=2))
    pickup(2, "Swapped In", DAY + timedelta(days=1), drop=9)
    pickup(3, "Already Made", DAY, status="executed")
    pending = job._pending(team.team_id)
    assert sorted(pending.days) == [DAY + timedelta(days=1), DAY + timedelta(days=2)]
    assert pending.add_only == (("Toumani Camara", DAY + timedelta(days=2)),)


def test_its_writes_are_audited_under_their_own_source(team):
    lineup = svc._audit_insert(team.user_id_id, team.team_id, DAY, 35, job.SOURCE,
                               [{"player_id": 4, "from_slot_id": 12, "to_slot_id": 13}], "k1")
    txn = svc._audit_insert(team.user_id_id, team.team_id, DAY, 35, job.SOURCE,
                            [{"player_id": 90, "action": "add", "name": "FA90"}], "k2", kind="transaction")
    rows = {r.id: (r.source, r.kind) for r in RosterMove.select().where(RosterMove.team == team.team_id)}
    assert rows == {lineup: ("daily_actions", "lineup"), txn: ("daily_actions", "transaction")}


def _column(table: str) -> bool:
    return db.execute_sql("SELECT 1 FROM information_schema.columns WHERE table_schema = 'usr' "
                          "AND table_name = %s AND column_name = 'daily_actions_level'", (table,)).fetchone() is not None


def test_0057_rollback_keeps_the_audit_rows_and_the_forward_file_restores_it(team):
    kept = svc._audit_insert(team.user_id_id, team.team_id, DAY, 35, job.SOURCE, [], "k")
    forward = (Path(MIGRATIONS_DIR) / "0057__daily_actions_level.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0057__daily_actions_level.rollback.sql").read_text()
    try:
        db.execute_sql(rollback)
        assert not _column("notification_preferences") and not _column("notification_team_preferences")
        assert RosterMove.get_by_id(kept).source == "scheduled"           # history stays, as the old CHECK allows
        with pytest.raises(IntegrityError):
            with db.atomic():
                svc._audit_insert(team.user_id_id, team.team_id, DAY, 35, job.SOURCE, [], "k2")
    finally:
        db.execute_sql(forward)
    assert _column("notification_preferences") and _column("notification_team_preferences")
    check = db.execute_sql("SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                           "WHERE conname = 'roster_moves_source_check'").fetchone()
    assert "'daily_actions'" in check[0] and check[1]
