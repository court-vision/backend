"""
Integration: migrations 0030 and 0037 against the real schema.

The unit layer decides what each decision does; here the tables decide what may be stored
and claimed: one open lane per spot and four per team (two starts at once included), the CHECKs, the lane id on scheduled
pickups, and the repository functions the service runs through `run_db` — the claim lease
(a team's due lanes together) and the writes-off release, a decision's writes landing
together and only for the claim that made it, a user's change fencing out a running
decision, other lanes' projections written only for an unclaimed lane, a settled lane
pickup moving the holder, the guard's lookup, the listing, the schedule read — plus the
rollbacks.
"""

import asyncio
import json
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.scheduled_pickups import ScheduledPickup
from db.models.stream_lanes import StreamLane, StreamLaneEvent, active_lane_holders
from db.models.teams import Team
from db.models.users import User
from schemas.stream_lane import StreamLaneUpdateReq
from services import scheduled_pickup_service as pickups
from services import stream_lane_service as svc

pytestmark = [pytest.mark.integration]

NOW = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
TUE = date(2026, 11, 17)
HOLDER, FA, FA2 = 4594268, 6450, 3059318


@pytest.fixture
def user(integration_db):
    return User.create(email="lanes@courtvision.dev", clerk_user_id="user_lanes", created_at=datetime.utcnow())


@pytest.fixture
def team(user):
    info = {"provider": "espn", "league_id": 552315826, "team_name": "GloatingSoap369", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="552315826GloatingSoap369", league_info=json.dumps(info))


def _lane(user, team, **kw):
    fields = dict(user=user.user_id, team=team.team_id, holder_player_id=HOLDER, holder_name="Anthony Edwards",
                  holder_team="MIN", original_player_id=HOLDER, original_name="Anthony Edwards", start_period=28,
                  end_period=34, end_date=date(2026, 11, 22), status="active", notify="email", next_period=29,
                  next_decision_at=NOW - timedelta(minutes=1), created_at=NOW, updated_at=NOW)
    fields.update(kw)
    return StreamLane.create(**fields)


def _pickup_fields(user, team, lane_id=None, add=FA, **kw):
    fields = dict(user=user.user_id, team=team.team_id, add_player_id=add, drop_player_id=HOLDER, add_name="Kawhi Leonard",
                  add_team="LAC", drop_name="Anthony Edwards", drop_team="MIN", scoring_period_id=29, nba_date=TUE,
                  not_before_at=NOW, deadline_at=NOW + timedelta(days=1), status="pending", created_at=NOW,
                  updated_at=NOW, lane_id=lane_id)
    fields.update(kw)
    return fields


# ---- the schema --------------------------------------------------------------------------------


def test_0030_and_0037_are_applied_by_the_chain(integration_db):
    ids = {r[0] for r in db.execute_sql("SELECT migration_id FROM public._yoyo_migration").fetchall()}
    assert {"0030__stream_lanes", "0037__stream_lanes_per_spot"} <= ids


def _new_lane(user, team, holder):
    return svc._insert_lane(dict(user=user.user_id, team=team.team_id, holder_player_id=holder, holder_name="K",
                                 original_player_id=holder, original_name="K", start_period=28, end_period=34,
                                 end_date=date(2026, 11, 22), next_period=28), dict(action="started"))


def test_one_open_lane_per_spot(user, team):
    first = _lane(user, team)
    with pytest.raises(svc.StreamLaneExists):
        _new_lane(user, team, HOLDER)
    StreamLane.update(status="paused").where(StreamLane.id == first.id).execute()
    with pytest.raises(IntegrityError), db.atomic():
        _lane(user, team)                                          # paused is still open
    second = _new_lane(user, team, FA)                             # another spot: a second lane
    assert StreamLaneEvent.select().where(StreamLaneEvent.lane == second.id).count() == 1
    StreamLane.update(status="ended", ended_at=NOW).where(StreamLane.id == first.id).execute()
    third = _new_lane(user, team, HOLDER)                          # the spot is free again
    assert [lane.id for lane in svc._open_lanes(team.team_id)] == [second.id, third.id]


def test_four_open_lanes_per_team(user, team):
    for holder in range(1, svc.MAX_LANES + 1):
        _new_lane(user, team, holder)
    with pytest.raises(svc.StreamLaneLimit):
        _new_lane(user, team, 99)
    assert StreamLane.select().count() == svc.MAX_LANES
    StreamLane.update(status="ended", ended_at=NOW).where(StreamLane.holder_player_id == 1).execute()
    _new_lane(user, team, 99)


def test_two_starts_at_once_still_stop_at_four(user, team, monkeypatch):
    """Two starts for different spots while the team has three lanes: the second waits for the first
    to commit, counts four and is refused, instead of both counting three and making five."""
    for holder in range(1, svc.MAX_LANES):
        _new_lane(user, team, holder)
    created, go = threading.Event(), threading.Event()
    real_create = StreamLane.create.__func__

    def create(cls, **fields):
        lane = real_create(cls, **fields)
        if threading.current_thread().name == "first":
            created.set()
            go.wait(5)                                              # hold the transaction open
        return lane
    monkeypatch.setattr(StreamLane, "create", classmethod(create))
    outcome = {}

    def start(name, holder):
        try:
            outcome[name] = _new_lane(user, team, holder).id
        except Exception as exc:
            outcome[name] = exc
        finally:
            if not db.is_closed():
                db.close()
    first = threading.Thread(target=start, args=("first", 10), name="first")
    first.start()
    assert created.wait(5)
    second = threading.Thread(target=start, args=("second", 11), name="second")
    second.start()
    second.join(0.5)
    blocked = second.is_alive()                                     # waiting on the team's lock
    go.set()
    first.join(5)
    second.join(5)
    assert blocked
    assert isinstance(outcome["first"], int) and isinstance(outcome["second"], svc.StreamLaneLimit)
    assert StreamLane.select().count() == svc.MAX_LANES


@pytest.mark.parametrize("field, value", [("status", "stopped"), ("notify", "sms")])
def test_the_lane_checks(user, team, field, value):
    with pytest.raises(IntegrityError), db.atomic():
        _lane(user, team, **{field: value})


def test_an_event_action_check_and_the_cascade(user, team):
    lane = _lane(user, team)
    with pytest.raises(IntegrityError), db.atomic():
        StreamLaneEvent.create(lane=lane.id, action="moved")
    StreamLaneEvent.create(lane=lane.id, action="keep")
    lane.delete_instance()
    assert StreamLaneEvent.select().count() == 0


def test_a_pickup_keeps_its_history_when_its_lane_goes(user, team):
    lane = _lane(user, team)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))
    lane.delete_instance()
    assert ScheduledPickup.get_by_id(pickup.id).lane_id is None


# ---- claims and decisions --------------------------------------------------------------------------


def test_the_claim_leases_due_active_lanes_only(user, team):
    due = _lane(user, team)
    other_team = Team.create(user_id=user.user_id, team_identifier="x2", league_info="{}")
    _lane(user, other_team, status="paused")
    third = Team.create(user_id=user.user_id, team_identifier="x3", league_info="{}")
    _lane(user, third, next_decision_at=NOW + timedelta(hours=1))
    (claimed,) = svc._claim_due_lanes(NOW, 10)
    assert claimed.id == due.id and claimed.attempts == 1 and claimed.reason == "in_progress"
    assert claimed.next_decision_at == NOW + svc.LEASE
    assert svc._claim_due_lanes(NOW, 10) == []                    # leased
    assert [r.id for r in svc._claim_due_lanes(NOW + svc.LEASE, 10)] == [due.id]


def test_the_claim_takes_a_teams_due_lanes_together(user, team):
    first = _lane(user, team, next_decision_at=NOW - timedelta(minutes=2))
    second = _lane(user, team, holder_player_id=FA)
    later = _lane(user, team, holder_player_id=FA2, next_decision_at=NOW + timedelta(hours=1))
    other_team = Team.create(user_id=user.user_id, team_identifier="x2", league_info="{}")
    _lane(user, other_team)
    claimed = svc._claim_due_lanes(NOW, 1)                         # one lane's team: both its due lanes
    assert sorted(r.id for r in claimed) == [first.id, second.id] and later.id not in {r.id for r in claimed}
    assert [r.team_id for r in svc._claim_due_lanes(NOW, 1)] == [other_team.team_id]


def test_writes_off_push_due_decisions_back(user, team):
    lane = _lane(user, team)
    assert svc._release_due_lanes(NOW, NOW + timedelta(minutes=10)) == 1
    lane = StreamLane.get_by_id(lane.id)
    assert lane.next_decision_at == NOW + timedelta(minutes=10) and lane.reason == "writes_disabled" and lane.attempts == 0


def test_a_decision_writes_together_and_only_for_its_claim(user, team):
    lane = _lane(user, team)
    (claimed,) = svc._claim_due_lanes(NOW, 1)
    fields = dict(next_period=30, next_decision_at=NOW + timedelta(days=1), reason=None,
                  projection={"days": [], "gain": 4.0})
    switch = dict(action="switch", scoring_period_id=29, nba_date=TUE, add_player_id=FA, add_name="Kawhi Leonard",
                  drop_player_id=HOLDER, drop_name="Anthony Edwards", reason="as_projected")
    assert svc._record_decision(lane.id, claimed.attempts - 1, NOW, fields, [switch],
                                _pickup_fields(user, team, lane.id)) is None   # a stale claim writes nothing
    assert ScheduledPickup.select().count() == 0 and StreamLane.get_by_id(lane.id).next_period == 29
    recorded = svc._record_decision(lane.id, claimed.attempts, NOW, fields, [switch], _pickup_fields(user, team, lane.id))
    pickup = ScheduledPickup.get_by_id(recorded.pickup_id)
    assert pickup.lane_id == lane.id and pickup.status == "pending"
    event = StreamLaneEvent.get(StreamLaneEvent.lane == lane.id)
    assert (event.action, event.pickup_id, event.reason) == ("switch", pickup.id, "as_projected")
    stored = StreamLane.get_by_id(lane.id)
    assert stored.next_period == 30 and stored.projection == {"days": [], "gain": 4.0} and stored.reason is None


def test_a_switch_to_a_player_already_scheduled_is_recorded_as_blocked(user, team):
    lane = _lane(user, team)
    ScheduledPickup.create(**_pickup_fields(user, team))           # the user's own pending pickup of the same player
    (claimed,) = svc._claim_due_lanes(NOW, 1)
    recorded = svc._record_decision(lane.id, claimed.attempts, NOW, dict(next_period=30),
                                    [dict(action="switch", add_player_id=FA, scoring_period_id=29)],
                                    _pickup_fields(user, team, lane.id))
    assert recorded.duplicate and recorded.pickup_id is None
    event = StreamLaneEvent.get(StreamLaneEvent.lane == lane.id)
    assert (event.action, event.reason, event.pickup_id) == ("blocked", "already_scheduled", None)
    assert StreamLane.get_by_id(lane.id).next_period == 30         # the lane still moves on


def test_a_new_lanes_projections_for_the_others_land_only_where_unclaimed(user, team):
    idle = _lane(user, team, next_decision_at=NOW + timedelta(hours=1))
    busy = _lane(user, team, holder_player_id=FA)
    (claimed,) = svc._claim_due_lanes(NOW, 1)                       # a decision is running for `busy`
    svc._set_projections([(idle.id, idle.attempts, {"lanes": 2}), (busy.id, busy.attempts, {"lanes": 2})], NOW)
    assert StreamLane.get_by_id(idle.id).projection == {"lanes": 2}
    assert StreamLane.get_by_id(busy.id).projection is None and claimed.id == busy.id


def test_a_users_change_fences_out_a_running_decision(user, team):
    lane = _lane(user, team)
    (claimed,) = svc._claim_due_lanes(NOW, 1)
    assert svc._lane_step(lane.id, None, NOW, dict(status="paused", next_decision_at=None), dict(action="paused"))
    assert svc._lane_step(lane.id, None, NOW, dict(status="active", next_decision_at=NOW), dict(action="resumed"))
    # Paused and resumed while the decision ran: it is active again, but no longer that claim's.
    assert svc._record_decision(lane.id, claimed.attempts, NOW, dict(next_period=30), [dict(action="keep")], None) is None
    assert svc._lane_step(lane.id, claimed.attempts, NOW, dict(reason="x")) is False
    actions = [e.action for e in StreamLaneEvent.select().where(StreamLaneEvent.lane == lane.id).order_by(StreamLaneEvent.id)]
    assert actions == ["paused", "resumed"]
    StreamLane.update(status="ended").where(StreamLane.id == lane.id).execute()
    assert svc._lane_step(lane.id, None, NOW, dict(notify="desk")) is False    # an ended lane is not the user's to change


# ---- pickups and the holder ---------------------------------------------------------------------------


def test_a_settled_lane_pickup_moves_the_holder_or_records_the_day(user, team):
    lane = _lane(user, team)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))
    StreamLaneEvent.create(lane=lane.id, action="switch", pickup_id=pickup.id, projected_player_id=FA2,
                           projected_name="Joel Embiid", reason="projected_gone")
    noted = svc._note_pickup_settled(lane.id, pickup.id, "executed", None, None, (FA, "Kawhi Leonard", "LAC"),
                                     (HOLDER, "Anthony Edwards"), 29, TUE, NOW)
    assert noted == {"lane_id": lane.id, "notify": "email", "projected": (FA2, "Joel Embiid"), "why": "projected_gone",
                     "last_day": 29 >= lane.end_period}
    moved = StreamLane.get_by_id(lane.id)
    assert (moved.holder_player_id, moved.holder_name, moved.holder_team) == (FA, "Kawhi Leonard", "LAC")
    assert moved.original_player_id == HOLDER
    svc._note_pickup_settled(lane.id, pickup.id, "skipped", "unavailable", "gone", (FA2, "J", "PHI"),
                             (FA, "Kawhi Leonard"), 30, TUE + timedelta(days=1), NOW)
    blocked = StreamLaneEvent.get((StreamLaneEvent.lane == lane.id) & (StreamLaneEvent.action == "blocked"))
    assert (blocked.reason, blocked.pickup_id, blocked.scoring_period_id) == ("unavailable", pickup.id, 30)
    assert StreamLane.get_by_id(lane.id).holder_player_id == FA
    assert svc._note_pickup_settled(lane.id + 99, pickup.id, "executed", None, None, (1, "x", None), (None, None),
                                    29, TUE, NOW) is None


def test_pending_and_adopted_pickups(user, team):
    lane = _lane(user, team)
    assert not svc._lane_has_pending(lane.id)
    pending = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))
    ScheduledPickup.create(**_pickup_fields(user, team, add=FA2, drop_player_id=None, scoring_period_id=31))
    assert svc._lane_has_pending(lane.id) and [p.id for p in svc._lane_pickups(lane.id)] == [pending.id]
    found = {(p.add_player_id, p.lane_id) for p in svc._pending_pickups(team.team_id)}
    assert found == {(FA, lane.id), (FA2, None)}
    ScheduledPickup.update(status="executed", executed_at=NOW).where(ScheduledPickup.id == pending.id).execute()
    assert svc._adopt_executed(lane.id, HOLDER, {FA}).id == pending.id
    assert svc._adopt_executed(lane.id, HOLDER, set()) is None      # the player isn't on the board either


def test_a_pause_cancels_the_lanes_pickup_before_the_executor_can_claim_it(user, team):
    lane = _lane(user, team, next_period=30)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))      # day 29's switch, due now
    asyncio.run(svc.StreamLaneService.update(team, None, lane.id, StreamLaneUpdateReq(status="paused")))
    paused = StreamLane.get_by_id(lane.id)
    assert (paused.status, paused.next_period) == ("paused", 29)                # day 29 is decided again on resume
    assert ScheduledPickup.get_by_id(pickup.id).status == "cancelled"
    assert pickups._claim_due(NOW, 4) == []


def test_a_pause_waits_while_the_lanes_pickup_is_being_made(user, team):
    lane = _lane(user, team, next_period=30)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))
    (claimed,) = pickups._claim_due(NOW, 4)                                     # an attempt holds it
    with pytest.raises(svc.StreamLaneBusy):
        asyncio.run(svc.StreamLaneService.update(team, None, lane.id, StreamLaneUpdateReq(status="paused")))
    assert StreamLane.get_by_id(lane.id).status == "active"
    assert ScheduledPickup.get_by_id(pickup.id).status == "pending" and claimed.id == pickup.id


def test_an_attempt_on_a_paused_lanes_pickup_cancels_it_unsent(user, team, monkeypatch):
    def no_further(team_id, user_id):
        raise AssertionError("the attempt went on to read the team and its board")
    monkeypatch.setattr(pickups, "_load_team_for_job", no_further)
    lane = _lane(user, team, status="paused")
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id))
    (claimed,) = pickups._claim_due(NOW, 4)
    assert asyncio.run(pickups.ScheduledPickupService._execute_one(claimed, NOW)) is None
    row = ScheduledPickup.get_by_id(pickup.id)
    assert (row.status, row.reason, row.audit_id) == ("cancelled", "lane_paused", None)
    assert StreamLane.get_by_id(lane.id).holder_player_id == HOLDER


def test_the_guard_sees_the_active_lanes_holders(user, team):
    lane = _lane(user, team)
    _lane(user, team, holder_player_id=FA)
    assert active_lane_holders(team.team_id) == {HOLDER, FA}
    StreamLane.update(status="paused").where(StreamLane.id == lane.id).execute()
    assert active_lane_holders(team.team_id) == {FA}


def test_the_listing(user, team):
    old = _lane(user, team, status="ended", ended_at=NOW - timedelta(days=30))
    recent = _lane(user, team, status="ended", ended_at=NOW - timedelta(days=1))
    current = _lane(user, team)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, current.id, status="executed"))
    StreamLaneEvent.create(lane=current.id, action="started")
    StreamLaneEvent.create(lane=current.id, action="switch", pickup_id=pickup.id)
    lanes, events, statuses = svc._list_lanes(team.team_id, NOW - timedelta(days=7))
    assert {lane.id for lane in lanes} == {recent.id, current.id} and old.id not in events
    assert [e.action for e in events[current.id]] == ["switch", "started"] and statuses == {pickup.id: "executed"}
    shaped = svc.lane_schema(current, events[current.id], statuses)
    assert shaped.events[0].pickup_status == "executed" and shaped.next_period == 29


def test_the_days_read_the_schedule(integration_db):
    from db.models.nba.games import Game
    from db.models.nba.teams import NBATeam
    for abbrev in ("MIN", "LAL", "BOS", "GSW"):
        NBATeam.get_or_create(id=abbrev, defaults={"name": abbrev, "abbreviation": abbrev, "city": abbrev,
                                                   "conference": "West", "division": "NW"})
    Game.create(game_id="g1", game_date=TUE, season="2026-27", home_team="MIN", away_team="GSW", start_time_et=time(19, 30))
    Game.create(game_id="g2", game_date=TUE, season="2026-27", home_team="BOS", away_team="LAL", start_time_et=time(19, 0))
    mon, tue, wed = svc._days(28, 30, 28, TUE - timedelta(days=1))
    assert (mon.teams, mon.first_tip) == (frozenset(), None)
    assert tue.period == 29 and tue.teams == {"MIN", "GSW", "BOS", "LAL"} and tue.first_tip == svc.et_at(TUE, time(19, 0))
    assert wed.nba_date == TUE + timedelta(days=1)


def test_0030_rollback_and_forward(user, team):
    forward = (Path(MIGRATIONS_DIR) / "0030__stream_lanes.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0030__stream_lanes.rollback.sql").read_text()
    lane = _lane(user, team)
    pickup = ScheduledPickup.create(**_pickup_fields(user, team, lane.id, status="executed"))
    db.execute_sql(rollback)
    assert db.execute_sql("SELECT to_regclass('usr.stream_lanes')").fetchone()[0] is None
    assert db.execute_sql("SELECT status FROM usr.scheduled_pickups WHERE id = %s", (pickup.id,)).fetchone() == ("executed",)
    db.execute_sql(forward)
    assert db.execute_sql("SELECT lane_id FROM usr.scheduled_pickups WHERE id = %s", (pickup.id,)).fetchone() == (None,)
    db.execute_sql((Path(MIGRATIONS_DIR) / "0037__stream_lanes_per_spot.sql").read_text())   # as the chain left it


def test_0037_rollback_and_forward(user, team):
    forward = (Path(MIGRATIONS_DIR) / "0037__stream_lanes_per_spot.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0037__stream_lanes_per_spot.rollback.sql").read_text()
    _lane(user, team)
    second = _lane(user, team, holder_player_id=FA)
    with pytest.raises(IntegrityError), db.atomic():
        db.execute_sql(rollback)                                    # two open lanes: one per team can't hold
    StreamLane.update(status="ended", ended_at=NOW).where(StreamLane.id == second.id).execute()
    db.execute_sql(rollback)
    with pytest.raises(IntegrityError), db.atomic():
        _lane(user, team, holder_player_id=FA2)                     # one open lane per team again
    db.execute_sql(forward)
    _lane(user, team, holder_player_id=FA2)
