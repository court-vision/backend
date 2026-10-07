"""
Integration: migration 0028 against the real schema.

The unit layer decides what each attempt does; here the table decides what may be
stored and claimed: the status CHECK, one pending row per (team, player), the
widened roster_moves source CHECK (with the auto-lineup index untouched), the
FKs, and the repository functions the service runs through `run_db` — the claim
lease, the writes-off release, the in-flight mark, settle / defer (both fenced by
the claim), cancel, list — plus the rollback, after the feature has run.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from db.models.users import User
from services import scheduled_pickup_service as svc

pytestmark = [pytest.mark.integration]

DAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 21, 23, 5, tzinfo=timezone.utc)
KAWHI, EDWARDS = 6450, 4594268


@pytest.fixture
def user(integration_db):
    return User.create(email="pickups@courtvision.dev", clerk_user_id="user_pickups", created_at=datetime.utcnow())


@pytest.fixture
def team(user):
    info = {"provider": "espn", "league_id": 426893737, "team_name": "Lvl. 3 Goblins", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="426893737Lvl. 3 Goblins", league_info=json.dumps(info))


def _pickup(user, team, *, add=KAWHI, drop=EDWARDS, not_before=NOW - timedelta(hours=1), **kw):
    fields = dict(user=user.user_id, team=team.team_id, add_player_id=add, drop_player_id=drop, add_name="Kawhi Leonard",
                  add_team="LAC", drop_name="Anthony Edwards", drop_team="MIN", scoring_period_id=3, nba_date=DAY,
                  not_before_at=not_before, deadline_at=NOW + timedelta(days=1), created_at=NOW, updated_at=NOW)
    fields.update(kw)
    return ScheduledPickup.create(**fields)


def _source_check_values():
    (definition,) = db.execute_sql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'roster_moves_source_check' AND conrelid = 'usr.roster_moves'::regclass"
    ).fetchone()
    return definition


# ---- 0028 ---------------------------------------------------------------------------


def test_0028_is_applied_by_the_chain(integration_db):
    ids = {r[0] for r in db.execute_sql("SELECT migration_id FROM public._yoyo_migration").fetchall()}
    assert "0028__scheduled_pickups" in ids
    assert "'scheduled'" in _source_check_values()


def test_scheduled_is_an_audit_source_outside_the_auto_index(user, team):
    for _ in range(2):   # two counted scheduled writes on one day both insert: not the auto dedup's business
        RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, scoring_period_id=3, source="scheduled",
                          kind="transaction", status="applied", moves=[{"player_id": KAWHI, "action": "add", "name": "K"}])
    assert RosterMove.select().where(RosterMove.source == "scheduled").count() == 2
    with pytest.raises(IntegrityError):
        RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, source="cron", status="applied", moves=[])


def test_one_pending_row_per_team_and_player(user, team):
    first = _pickup(user, team)
    with pytest.raises(IntegrityError):
        _pickup(user, team, scoring_period_id=4)
    _pickup(user, team, add=99)                                     # another player is fine
    svc._cancel_pickup(team.team_id, first.id, NOW)
    _pickup(user, team)                                             # a settled row frees the slot


def test_the_status_check(user, team):
    with pytest.raises(IntegrityError):
        _pickup(user, team, status="queued")


def test_fks_cascade_and_null(user, team):
    audit = RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, source="scheduled", kind="transaction",
                              status="applied", moves=[])
    row = _pickup(user, team, status="executed", audit_id=audit.id, executed_at=NOW)
    audit.delete_instance()
    assert ScheduledPickup.get_by_id(row.id).audit_id is None
    team.delete_instance()
    assert ScheduledPickup.select().count() == 0


# ---- repository functions ---------------------------------------------------------------


def test_claim_takes_only_due_unleased_rows_in_order_and_leases_them(user, team):
    late = _pickup(user, team, add=1, not_before=NOW - timedelta(minutes=5))
    early = _pickup(user, team, add=2, not_before=NOW - timedelta(hours=2))
    _pickup(user, team, add=3, not_before=NOW + timedelta(hours=1))                          # not due yet
    _pickup(user, team, add=4, next_attempt_at=NOW + timedelta(minutes=30))                  # deferred
    _pickup(user, team, add=5, not_before=NOW - timedelta(minutes=3),
            next_attempt_at=NOW - timedelta(minutes=1))                                      # deferral elapsed
    _pickup(user, team, add=6, status="cancelled")

    rows = svc._claim_due(NOW, limit=2)
    assert [r.add_player_id for r in rows] == [2, 1]                   # soonest first, the limit respected
    for r in rows:
        assert r.attempts == 1 and r.next_attempt_at == NOW + svc.LEASE and r.last_attempt_at == NOW
        assert r.reason == "in_progress"

    assert [r.add_player_id for r in svc._claim_due(NOW, limit=10)] == [5]   # the leased two are invisible
    assert svc._claim_due(NOW, limit=10) == []
    stored = ScheduledPickup.get_by_id(early.id)
    assert stored.attempts == 1 and stored.status == "pending"
    assert ScheduledPickup.get_by_id(late.id).attempts == 1
    # after the lease the rows are claimable again, and the attempt count keeps growing
    again = svc._claim_due(NOW + svc.LEASE, limit=10)
    assert [r.add_player_id for r in again] == [2, 1, 5]
    assert all(r.attempts == 2 for r in again)


def test_a_row_that_used_up_its_attempts_settles_when_due_instead_of_being_claimed(user, team):
    spent = _pickup(user, team, add=1, attempts=svc.MAX_ATTEMPTS, reason="in_progress",
                    next_attempt_at=NOW - timedelta(minutes=1))                # its last attempt died after the claim
    last = _pickup(user, team, add=2, attempts=svc.MAX_ATTEMPTS - 1)
    rows = svc._claim_due(NOW, limit=10)
    assert [(r.add_player_id, r.status, r.reason) for r in rows] == [(1, "failed", "max_attempts"),
                                                                     (2, "pending", "in_progress")]
    stored = ScheduledPickup.get_by_id(spent.id)
    assert (stored.status, stored.detail, stored.attempts, stored.next_attempt_at) == ("failed", "in_progress",
                                                                                      svc.MAX_ATTEMPTS, None)
    # the other row's claim was its last: when its lease runs out it settles too, and neither comes back
    assert [(r.id, r.status) for r in svc._claim_due(NOW + svc.LEASE, limit=10)] == [(last.id, "failed")]
    assert svc._claim_due(NOW + 2 * svc.LEASE, limit=10) == []


def test_a_row_whose_write_is_out_is_claimed_once_past_the_budget_to_confirm_it(user, team):
    sent = _pickup(user, team, attempts=svc.MAX_ATTEMPTS, audit_id=_audit(user, team).id, reason="writer_unavailable",
                   next_attempt_at=NOW - timedelta(minutes=1))                 # its last attempt's write got no answer
    (confirming,) = svc._claim_due(NOW, limit=10)
    assert (confirming.status, confirming.attempts) == ("pending", svc.MAX_ATTEMPTS + 1)
    (settled,) = svc._claim_due(NOW + svc.LEASE, limit=10)                       # ... and that attempt died too
    assert (settled.id, settled.status, settled.reason, settled.detail) == (sent.id, "failed", "max_attempts",
                                                                            "in_progress")


def test_release_pushes_due_rows_without_counting_an_attempt(user, team):
    row = _pickup(user, team)
    assert svc._release_due(NOW, NOW + svc.RETRY) == 1
    stored = ScheduledPickup.get_by_id(row.id)
    assert (stored.attempts, stored.next_attempt_at, stored.reason) == (0, NOW + svc.RETRY, "writes_disabled")
    assert svc._claim_due(NOW, limit=10) == []


def _audit(user, team, status="failed"):
    return RosterMove.create(user=user.user_id, team=team.team_id, nba_date=DAY, source="scheduled", kind="transaction",
                             status=status, moves=[])


def test_settle_and_defer(user, team):
    row = _pickup(user, team)
    (claimed,) = svc._claim_due(NOW, limit=1)
    assert svc._defer_row(claimed.id, claimed.attempts, NOW, NOW + svc.RETRY, "add_locked", "Kawhi is locked", None)
    stored = ScheduledPickup.get_by_id(row.id)
    assert (stored.status, stored.reason, stored.detail, stored.next_attempt_at) == ("pending", "add_locked", "Kawhi is locked", NOW + svc.RETRY)

    audit = _audit(user, team, "applied")
    assert svc._settle_row(row.id, claimed.attempts, NOW, status="executed", reason=None, detail=None, audit_id=audit.id,
                           lineup_audit_id=None, seated_slot_id=11, executed_at=NOW)
    stored = ScheduledPickup.get_by_id(row.id)
    assert (stored.status, stored.audit_id, stored.seated_slot_id, stored.executed_at) == ("executed", audit.id, 11, NOW)
    assert stored.next_attempt_at is None and stored.reason is None


def test_the_in_flight_mark_takes_only_the_claim_that_holds_the_row(user, team):
    row = _pickup(user, team)
    (claimed,) = svc._claim_due(NOW, limit=1)
    first, second = _audit(user, team), _audit(user, team)
    lease = NOW + timedelta(minutes=9)
    assert not svc._mark_in_flight(row.id, claimed.attempts + 1, first.id, NOW, lease)    # another claim's token
    assert svc._mark_in_flight(row.id, claimed.attempts, first.id, NOW, lease)
    stored = ScheduledPickup.get_by_id(row.id)
    assert (stored.status, stored.audit_id, stored.next_attempt_at) == ("pending", first.id, lease)
    assert not svc._mark_in_flight(row.id, claimed.attempts, second.id, NOW, lease)       # a write is already out
    assert ScheduledPickup.get_by_id(row.id).audit_id == first.id
    # the mark survives a deferral, so the next attempt only confirms
    assert svc._defer_row(row.id, claimed.attempts, NOW, NOW + svc.WRITER_RETRY, "writer_unavailable", None, first.id)
    assert ScheduledPickup.get_by_id(row.id).audit_id == first.id


def test_a_cancelled_row_never_takes_the_in_flight_mark(user, team):
    row = _pickup(user, team)
    (claimed,) = svc._claim_due(NOW, limit=1)
    svc._cancel_pickup(team.team_id, row.id, NOW + svc.LEASE)                             # the claim's lease ran out
    assert not svc._mark_in_flight(row.id, claimed.attempts, _audit(user, team).id, NOW, NOW + svc.LEASE)
    assert ScheduledPickup.get_by_id(row.id).audit_id is None


def test_an_attempt_that_lost_its_row_can_neither_settle_nor_defer_it(user, team):
    row = _pickup(user, team)
    (stale,) = svc._claim_due(NOW, limit=1)
    (current,) = svc._claim_due(NOW + svc.LEASE, limit=1)                                 # claimed again after the lease
    assert not svc._settle_row(row.id, stale.attempts, NOW, status="skipped", reason="unavailable")
    assert not svc._defer_row(row.id, stale.attempts, NOW, NOW + svc.RETRY, "add_on_waivers", None, None)
    stored = ScheduledPickup.get_by_id(row.id)
    assert (stored.status, stored.attempts, stored.next_attempt_at) == ("pending", 2, NOW + 2 * svc.LEASE)
    assert svc._settle_row(row.id, current.attempts, NOW, status="skipped", reason="unavailable")
    assert not svc._settle_row(row.id, current.attempts, NOW, status="failed", reason="espn_rejected")   # settled once
    assert ScheduledPickup.get_by_id(row.id).status == "skipped"


def test_cancel(user, team):
    other = Team.create(user_id=user.user_id, team_identifier="x", league_info="{}")
    row = _pickup(user, team)
    assert svc._cancel_pickup(other.team_id, row.id, NOW) is None
    prior, stored = svc._cancel_pickup(team.team_id, row.id, NOW)
    assert prior == "pending" and stored.status == "cancelled" and stored.next_attempt_at is None
    prior, stored = svc._cancel_pickup(team.team_id, row.id, NOW)
    assert prior == "cancelled"


def test_cancel_waits_out_an_attempt_that_holds_the_row(user, team):
    row = _pickup(user, team)
    (claimed,) = svc._claim_due(NOW, limit=1)
    prior, stored = svc._cancel_pickup(team.team_id, row.id, NOW + timedelta(minutes=1))      # the attempt is running
    assert (prior, stored.status, stored.reason) == ("in_progress", "pending", "in_progress")
    # a deferral ends the attempt, and the row can be cancelled again
    assert svc._defer_row(row.id, claimed.attempts, NOW, NOW + svc.RETRY, "add_on_waivers", None, None)
    prior, stored = svc._cancel_pickup(team.team_id, row.id, NOW + timedelta(minutes=1))
    assert (prior, stored.status) == ("pending", "cancelled")


def test_cancel_goes_through_once_a_dead_attempts_lease_ran_out_unless_its_write_is_out(user, team):
    quiet = _pickup(user, team, add=1)
    sent = _pickup(user, team, add=2)
    claimed = {r.add_player_id: r for r in svc._claim_due(NOW, limit=2)}
    assert svc._mark_in_flight(sent.id, claimed[2].attempts, _audit(user, team).id, NOW, NOW + svc.LEASE)
    later = NOW + svc.LEASE + timedelta(minutes=1)                                            # both workers died
    prior, stored = svc._cancel_pickup(team.team_id, quiet.id, later)
    assert (prior, stored.status) == ("pending", "cancelled")
    prior, stored = svc._cancel_pickup(team.team_id, sent.id, later)
    assert (prior, stored.status) == ("in_progress", "pending") and stored.audit_id is not None


def test_list_keeps_pending_and_the_recent_week(user, team):
    _pickup(user, team, add=1)
    _pickup(user, team, add=2, status="executed", updated_at=NOW - timedelta(days=2))
    _pickup(user, team, add=3, status="skipped", updated_at=NOW - timedelta(days=9))
    rows = svc._list_pickups(team.team_id, NOW - timedelta(days=7))
    assert sorted(r.add_player_id for r in rows) == [1, 2]


def test_window_reads_the_schedule(user, team):
    from db.models.nba.games import Game
    from db.models.nba.teams import NBATeam
    from datetime import time
    for abbrev in ("MIN", "LAL", "BOS", "DEN"):
        NBATeam.get_or_create(id=abbrev, defaults={"name": abbrev, "abbreviation": abbrev, "city": abbrev,
                                                   "conference": "West", "division": "NW"})
    Game.create(game_id="g1", game_date=DAY - timedelta(days=1), season="2026-27", home_team="MIN", away_team="LAL",
                start_time_et=time(19, 30))
    Game.create(game_id="g2", game_date=DAY, season="2026-27", home_team="BOS", away_team="DEN", start_time_et=time(19, 0))
    w = svc._window_for(DAY, "MIN")
    assert w.rule == "rollover_into_day" and w.deadline_at == svc.et_at(DAY, time(19, 0))
    assert svc._window_for(DAY, "BOS").rule == "first_tip_prev"


# ---- rollback -----------------------------------------------------------------------------


def test_0028_rollback_keeps_the_scheduled_audit_rows_and_the_forward_file_restores_it(user, team):
    forward = (Path(MIGRATIONS_DIR) / "0028__scheduled_pickups.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0028__scheduled_pickups.rollback.sql").read_text()

    def table_exists():
        return db.execute_sql("SELECT to_regclass('usr.scheduled_pickups')").fetchone()[0] is not None

    ran = _audit(user, team, "applied")                      # the feature ran before it was rolled back
    _pickup(user, team, status="executed", audit_id=ran.id, executed_at=NOW)
    assert table_exists() and "'scheduled'" in _source_check_values()
    db.execute_sql(rollback)
    assert not table_exists() and "'scheduled'" not in _source_check_values()
    assert RosterMove.get_by_id(ran.id).source == "scheduled"                # the audit history stays
    with pytest.raises(IntegrityError), db.atomic():
        _audit(user, team)                                                    # but a new 'scheduled' write is refused
    db.execute_sql(forward)
    assert table_exists() and "'scheduled'" in _source_check_values()
    assert "NOT VALID" not in _source_check_values()
