"""
Integration: migration 0048 (usr.roster_protections, the roster lock) against the real schema.

The unit layer decides what each check does; here the table decides what may be stored (one row
per team and player, cascading with the team), and the repository functions the service runs
through `run_db` are run for real: the idempotent insert, the reads by team and for several
teams, the delete, and the lookups of what protecting cancels (a pending pickup dropping him
that no lane owns; an open lane whose spot he holds). Then protecting end to end, with ESPN's
board stubbed: the lane stopped and the pickup cancelled in the database. Plus the rollback.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.roster_protections import RosterProtection
from db.models.scheduled_pickups import ScheduledPickup
from db.models.stream_lanes import StreamLane
from db.models.teams import Team
from db.models.users import User
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from services import roster_protection as svc

pytestmark = [pytest.mark.integration]

NOW = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
THU = date(2026, 11, 19)
EDWARDS, JOKIC, KAWHI = 4594268, 3112335, 6450


@pytest.fixture
def user(integration_db):
    return User.create(email="lock@courtvision.dev", clerk_user_id="user_lock", created_at=datetime.utcnow())


def _team(user, name="GloatingSoap369"):
    info = {"provider": "espn", "league_id": 552315826, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"552315826{name}", league_info=json.dumps(info))


@pytest.fixture
def team(user):
    return _team(user)


def _pickup(user, team, add, *, drop=EDWARDS, lane_id=None, status="pending", add_name="Kawhi Leonard"):
    """One pending pickup per player added (the table's own rule), so each takes its own `add`."""
    return ScheduledPickup.create(
        user=user.user_id, team=team.team_id, add_player_id=add, drop_player_id=drop, add_name=add_name,
        add_team="LAC", drop_name="Anthony Edwards", drop_team="MIN", scoring_period_id=31, nba_date=THU,
        not_before_at=NOW, deadline_at=NOW + timedelta(days=3), status=status, created_at=NOW, updated_at=NOW,
        lane_id=lane_id)


def _lane(user, team, *, holder=EDWARDS, status="active"):
    return StreamLane.create(
        user=user.user_id, team=team.team_id, holder_player_id=holder, holder_name="Anthony Edwards",
        holder_team="MIN", original_player_id=holder, original_name="Anthony Edwards", start_period=28,
        end_period=34, end_date=date(2026, 11, 22), status=status, notify="email", next_period=29,
        next_decision_at=NOW, created_at=NOW, updated_at=NOW)


def test_0048_is_applied_by_the_chain(integration_db):
    ids = {r[0] for r in db.execute_sql("SELECT migration_id FROM public._yoyo_migration").fetchall()}
    assert "0048__roster_protections" in ids


def test_one_row_per_team_and_player_and_the_insert_is_idempotent(user, team):
    row, made = svc._insert(team.team_id, user.user_id, EDWARDS, 1630162, "Anthony Edwards", "MIN")
    assert made and row.id
    again, made_again = svc._insert(team.team_id, user.user_id, EDWARDS, 1630162, "Anthony Edwards", "MIN")
    assert (again.id, made_again) == (row.id, False)
    with pytest.raises(IntegrityError), db.atomic():
        RosterProtection.create(team_id=team.team_id, user_id=user.user_id, player_id=EDWARDS, player_name="A")
    # Another team protects the same player on its own
    other = _team(user, "Other")
    svc._insert(other.team_id, user.user_id, EDWARDS, None, "Anthony Edwards", None)
    assert svc._ids(team.team_id) == frozenset({EDWARDS}) and svc._ids(other.team_id) == frozenset({EDWARDS})


def test_the_reads_by_team_and_for_several_teams(user, team):
    other = _team(user, "Other")
    svc._insert(team.team_id, user.user_id, JOKIC, 203999, "Nikola Jokic", "DEN")
    svc._insert(team.team_id, user.user_id, EDWARDS, 1630162, "Anthony Edwards", "MIN")
    svc._insert(other.team_id, user.user_id, KAWHI, 202695, "Kawhi Leonard", "LAC")
    assert [r.player_name for r in svc._rows(team.team_id)] == ["Anthony Edwards", "Nikola Jokic"]
    by_team = asyncio.run(svc.for_teams([team.team_id, other.team_id]))
    assert [p.name for p in by_team[team.team_id]] == ["Anthony Edwards", "Nikola Jokic"]
    assert by_team[other.team_id] == [svc.Protected(KAWHI, 202695, "Kawhi Leonard", "LAC")]
    assert asyncio.run(svc.ids(team.team_id)) == frozenset({EDWARDS, JOKIC})


def test_unprotecting_deletes_the_row_and_the_team_takes_its_rows_with_it(user, team):
    svc._insert(team.team_id, user.user_id, EDWARDS, None, "Anthony Edwards", None)
    assert svc._delete(team.team_id, EDWARDS).player_id == EDWARDS
    assert svc._delete(team.team_id, EDWARDS) is None and svc._ids(team.team_id) == frozenset()
    svc._insert(team.team_id, user.user_id, EDWARDS, None, "Anthony Edwards", None)
    Team.delete().where(Team.team_id == team.team_id).execute()
    assert RosterProtection.select().count() == 0


def test_what_protecting_cancels_is_his_pending_pickups_and_the_open_lanes_on_his_spot(user, team):
    lane = _lane(user, team)
    paused = _lane(user, team, holder=JOKIC, status="paused")
    _lane(user, team, holder=EDWARDS, status="ended")
    mine = _pickup(user, team, KAWHI)
    _pickup(user, team, KAWHI + 1, lane_id=lane.id)                  # the lane's own: goes with the lane's stop
    _pickup(user, team, KAWHI + 2, status="executed")
    _pickup(user, team, KAWHI + 3, drop=JOKIC)
    assert [p.id for p in svc._pickups_dropping(team.team_id, EDWARDS)] == [mine.id]
    assert [ln.id for ln in svc._lanes_holding(team.team_id, EDWARDS)] == [lane.id]
    assert [ln.id for ln in svc._lanes_holding(team.team_id, JOKIC)] == [paused.id]


def test_protecting_stops_the_lane_and_cancels_the_pickup_in_the_database(user, team, monkeypatch):
    from services import providers

    lane = _lane(user, team)
    lane_pickup = _pickup(user, team, KAWHI, lane_id=lane.id)
    mine = _pickup(user, team, KAWHI + 1, add_name="Joel Embiid")
    board = LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=1, nba_date="2026-11-16",
                        scoring_period_id=28, roster_version="v", fetched_at="now", can_write=True,
                        players=[LineupPlayer(player_id=EDWARDS, nba_player_id=1630162, name="Anthony Edwards",
                                              team="MIN", lineup_slot_id=12, lineup_slot="BE",
                                              eligible_slot_ids=[12], eligible_slots=["BE"])])

    class Adapter:
        async def read_lineup(self, team_id, league_info, **kw):
            return board
    monkeypatch.setattr(providers, "get_provider_adapter", lambda provider: Adapter())
    owned = SimpleNamespace(team_id=team.team_id, user_id=user.user_id, league_info_json=team.league_info,
                            league_id=None, league=None)
    league = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="T", year=2027)

    resp = asyncio.run(svc.RosterProtectionService.protect(owned, league, EDWARDS))

    assert [(c.kind, c.id) for c in resp.data.cancelled] == [("lane", lane.id), ("pickup", mine.id)]
    assert resp.data.not_cancelled == []
    stopped = StreamLane.get_by_id(lane.id)
    assert (stopped.status, stopped.reason) == ("ended", "stopped_for_protected")
    assert ScheduledPickup.get_by_id(mine.id).status == "cancelled"
    assert ScheduledPickup.get_by_id(lane_pickup.id).status == "cancelled"
    assert svc._ids(team.team_id) == frozenset({EDWARDS})


def test_0048_rollback_and_forward(user, team):
    forward = (Path(MIGRATIONS_DIR) / "0048__roster_protections.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0048__roster_protections.rollback.sql").read_text()
    svc._insert(team.team_id, user.user_id, EDWARDS, None, "Anthony Edwards", None)
    db.execute_sql(rollback)
    assert db.execute_sql("SELECT to_regclass('usr.roster_protections')").fetchone()[0] is None
    db.execute_sql(forward)
    assert svc._ids(team.team_id) == frozenset()
