"""
Integration: migrations 0020 and 0022 against the real schema.

The unit layer decides *what* to write; here the partial unique index decides
what may be written twice. One counted auto attempt (applied /
applied_unverified / noop) per team per fantasy day, while rejected / failed
rows — and every manual row — may repeat. Also the repository functions the
editor service runs through `run_db`, the new preference column defaults, and
0022's `kind` column (lineup | transaction) with its CHECK and rollback.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from peewee import IntegrityError

from api.deps import UserContext
from api.v1.internal.notifications import upsert_team_preference
from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.teams import Team
from db.models.users import User
from schemas.notifications import NotificationTeamPreferenceReq
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


def test_team_preference_upsert_only_writes_the_fields_the_caller_sent(user, team):
    """The request model promises a partial update; an omitted field must survive it."""
    upsert = upsert_team_preference.__wrapped__      # the sync body, without the run_db boundary
    ctx = UserContext(user_id=user.user_id)

    def stored():
        return NotificationTeamPreference.get(
            (NotificationTeamPreference.user == user.user_id)
            & (NotificationTeamPreference.team_id == team.team_id)
        )

    upsert(team.team_id, NotificationTeamPreferenceReq(auto_lineup_enabled=True, alert_minutes_before=45), ctx)
    row = stored()
    assert (row.auto_lineup_enabled, row.alert_minutes_before) == (True, 45)
    assert row.lineup_alerts_enabled is None                      # never sent, so still inheriting

    resp = upsert(team.team_id, NotificationTeamPreferenceReq(lineup_alerts_enabled=True), ctx)
    row = stored()
    assert row.auto_lineup_enabled is True and row.alert_minutes_before == 45
    assert row.lineup_alerts_enabled is True
    assert resp.data.auto_lineup_enabled is True                  # and the response reports the merge

    upsert(team.team_id, NotificationTeamPreferenceReq(auto_lineup_enabled=None), ctx)
    assert stored().auto_lineup_enabled is None                   # an explicit null still clears it
    assert NotificationTeamPreference.select().count() == 1


# ---- 0022: kind ----------------------------------------------------------------------


def _kind_column():
    row = db.execute_sql(
        "SELECT column_default, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'usr' AND table_name = 'roster_moves' AND column_name = 'kind'"
    ).fetchone()
    return row


def _kind_check():
    return db.execute_sql(
        "SELECT 1 FROM pg_constraint WHERE conname = 'roster_moves_kind_check' AND conrelid = 'usr.roster_moves'::regclass"
    ).fetchone() is not None


def test_0022_is_applied_by_the_chain(integration_db):
    ids = {r[0] for r in db.execute_sql("SELECT migration_id FROM public._yoyo_migration").fetchall()}
    assert "0022__roster_moves_kind" in ids
    default, nullable = _kind_column()
    assert default.startswith("'lineup'") and nullable == "NO" and _kind_check()


def test_existing_rows_are_lineup_writes(user, team):
    """A row written without the column (every pre-0022 row) reads back as a lineup write."""
    (row_id,) = db.execute_sql(
        "INSERT INTO usr.roster_moves (user_id, team_id, nba_date, source, status, moves) "
        "VALUES (%s, %s, %s, 'manual', 'applied', '[]'::jsonb) RETURNING id",
        (user.user_id, team.team_id, DAY),
    ).fetchone()
    assert RosterMove.get_by_id(row_id).kind == "lineup"
    assert _row(user, team, "auto", "applied").kind == "lineup"          # the model default matches


def test_two_manual_transactions_on_one_day_both_insert(user, team):
    moves = [{"player_id": 6450, "action": "add", "name": "Kawhi Leonard"},
             {"player_id": 3112335, "action": "drop", "name": "Nikola Jokic"}]
    for _ in range(2):
        RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, scoring_period_id=1,
                          source="manual", kind="transaction", status="applied", moves=moves)
    rows = list(RosterMove.select().where(RosterMove.kind == "transaction"))
    assert len(rows) == 2 and rows[0].moves == moves


def test_the_check_refuses_an_unknown_kind(user, team):
    with pytest.raises(IntegrityError):
        RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, source="manual",
                          kind="other", status="applied", moves=[])


def test_transaction_audit_insert_round_trip(user, team):
    audit_id = svc._audit_insert(user.user_id, team.team_id, DAY, 1, "manual",
                                 [{"player_id": 6450, "action": "add", "name": "Kawhi Leonard"}],
                                 "21:txn:1:v1:6450:0", kind="transaction")
    row = RosterMove.get_by_id(audit_id)
    assert (row.kind, row.status, row.error, row.idempotency_key) == ("transaction", "failed", "in_flight", "21:txn:1:v1:6450:0")
    assert row.moves == [{"player_id": 6450, "action": "add", "name": "Kawhi Leonard"}]
    assert svc._auto_counted_today(team.team_id, DAY) is False          # manual, never the auto dedup's business
    svc._audit_update(audit_id, "applied", provider_status=200)
    assert RosterMove.get_by_id(audit_id).status == "applied"


def test_0022_rollback_applies_and_the_forward_file_restores_it(integration_db):
    forward = (Path(MIGRATIONS_DIR) / "0022__roster_moves_kind.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0022__roster_moves_kind.rollback.sql").read_text()
    assert _kind_column() is not None and _kind_check()
    try:
        db.execute_sql(rollback)
        assert _kind_column() is None and not _kind_check()
    finally:
        db.execute_sql(forward)                                          # the chain's state is untouched
    assert _kind_column() is not None and _kind_check()
    db.execute_sql(forward)                                              # idempotent: a second apply is a no-op
