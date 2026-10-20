"""
services.stream_lane_service without ESPN or Postgres: the board, the pool and the schedule are
fakes, and every repository function is an in-memory stand-in recording what was written. The
pickups' timing rule is the real one (`scheduled_pickup_service._window_for`) over the fake
schedule, so the lane's decision times follow the executor's whatever that rule becomes.

Covers starting a lane (its gates and what is stored), the dry run, a decision's every branch
(switch, keep, waiting on its pickup, before the window, a provider error, the holder gone or
adopted, an expired connection, missed days, the last day, past the end), a team's lanes deciding
together, the tick wrapper, the user's pause / resume / stop, and what a settled lane pickup tells
the lane.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.errors import NotFoundError, ProviderAuthError
from db.models.nba.games import Game
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp
from schemas.lineup_editor import AcquisitionState, LineupPlayer, LineupState
from schemas.stream_lane import StreamLanePlanReq, StreamLaneStartReq, StreamLaneUpdateReq
from services import stream_lane_service as svc
from services.scheduled_pickup_service import et_at

PG, C, UT, BE, IR = 0, 4, 11, 12, 13
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
HOLDER, GUARD, FA, FA2, BIG = 1, 2, 10, 11, 3
MON = date(2026, 11, 16)                                     # ESPN day 28; the matchup runs to Sun 11/22 (day 34)
NOON = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)   # Mon noon EST, before the 7 PM tip
TUE = MON + timedelta(days=1)


def tip(d: date) -> datetime:
    return et_at(d, time(19, 0))


def opens(d: date) -> datetime:
    """When a pickup for day `d` can first be made: the executor's own rule over the fake schedule
    (the previous day's first tip-off)."""
    return svc._window_for(d).not_before_at


def lineup_player(pid, name, slot, team, *, eligible, avg=20.0):
    return LineupPlayer(player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot),
                        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible], avg_points=avg)


def board(players=None, *, period=28, nba_date="2026-11-16", acq="default", can_write=True, reason=None):
    if acq == "default":
        acq = AcquisitionState(per="matchup", limit=4, matchup_period_id=5, matchup_start=MON,
                               matchup_end=date(2026, 11, 22), matchup_used=0, waiver_hours=24)
    if players is None:
        players = [lineup_player(HOLDER, "Holder", UT, "MIA", eligible=(UT, BE), avg=15),
                   lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE), avg=30)]
    return LineupState(provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
                       scoring_period_id=period, current_scoring_period_id=period, final_scoring_period_id=167,
                       slot_counts={"0": 1, "11": 1, "12": 3, "13": 1}, players=players, can_write=can_write,
                       write_blocked_reason=reason, roster_version="v", fetched_at="now", acquisitions=acq)


def free_agent(pid, name, team, *, avg=25.0, status="free_agent", until=None, positions=("SF", "UT1"), pos=3):
    return PlayerResp(player_id=pid, name=name, avg_points=avg, team=team, valid_positions=list(positions),
                      injured=False, acquisition_status=status, waivers_until=until, default_position_id=pos)


def lane(**kw):
    base = dict(id=7, team_id=21, user_id=11, holder_player_id=HOLDER, holder_name="Holder", holder_team="MIA",
                original_player_id=HOLDER, original_name="Holder", start_period=28, end_period=34,
                end_date=date(2026, 11, 22), status="active", notify="email", next_period=29,
                next_decision_at=tip(MON) - timedelta(minutes=1), attempts=3, reason="in_progress", detail=None,
                projection=None, created_at=NOON, updated_at=NOON, ended_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def h(monkeypatch):
    # Who plays: the holder (MIA) Mon, Wed, Sat; the free agent (LAL) Tue, Thu; nobody else matters.
    schedule = {MON: {"MIA", "DEN"}, MON + timedelta(1): {"LAL"}, MON + timedelta(2): {"MIA"},
                MON + timedelta(3): {"LAL"}, MON + timedelta(5): {"MIA"}}
    h = SimpleNamespace(board=board(), pool=[free_agent(FA, "Free Agent", "LAL")], reads=0, schedule=schedule,
                        pending=[], open_lanes=[], inserted=[], recorded=[], steps=[], held=True,
                        lane_pending=False, adopted=None, team=(LEAGUE, {}), cancelled=[], cancel_prior="pending",
                        lane_pickups=[], day_adds=0, projections=[], lane_pending_ids=set())

    class Adapter:
        def capabilities(self, league_info):
            return SimpleNamespace(transactions=True)

        async def read_lineup(self, team_id, league_info, *, fallback_slot_counts=None, **kw):
            h.reads += 1
            if isinstance(h.board, Exception):
                raise h.board
            return h.board

        async def get_free_agents(self, league_info, fa_count, team_id=None):
            return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=list(h.pool))

    def days(start, end, base_period, base_date):
        out = []
        for p in range(start, end + 1):
            d = base_date + timedelta(days=p - base_period)
            teams = frozenset(h.schedule.get(d, set()))
            out.append(svc.LaneDay(p, d, teams, tip(d) if teams else None))
        return out

    def first_tip(d):
        return time(19, 0) if h.schedule.get(d) else None

    def record(lane_id, attempts, now, lane_fields, events, pickup):
        h.recorded.append(SimpleNamespace(lane_id=lane_id, attempts=attempts, fields=lane_fields, events=events,
                                          pickup=pickup))
        return svc.Recorded(pickup_id=501 if pickup else None) if h.held else None

    def step(lane_id, attempts, now, lane_fields, event=None):
        h.steps.append(SimpleNamespace(lane_id=lane_id, attempts=attempts, fields=lane_fields, event=event))
        return h.held

    def insert(lane_fields, event_fields):
        h.inserted.append((lane_fields, event_fields))
        return SimpleNamespace(id=7, team_id=21, user_id=11, ended_at=None, reason=None, detail=None,
                               **{k: v for k, v in lane_fields.items() if k not in ("user", "team")})

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def direct_run_cpu(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def day_adds(league_info, espn_team_id, period):
        return h.day_adds

    def cancel(team_id, pickup_id, now):
        h.cancelled.append(pickup_id)
        return h.cancel_prior, SimpleNamespace(id=pickup_id, status="cancelled")

    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "run_cpu", direct_run_cpu)
    monkeypatch.setattr(svc, "get_provider_adapter", lambda provider: Adapter())
    monkeypatch.setattr(svc, "_load_team_for_job", lambda team_id, user_id: h.team)
    monkeypatch.setattr(svc, "_roster_slots", lambda team: {})
    monkeypatch.setattr(svc, "_days", days)
    monkeypatch.setattr(Game, "get_earliest_game_time_on_date", staticmethod(first_tip))
    monkeypatch.setattr(Game, "get_teams_playing_on_date", staticmethod(lambda d: set(h.schedule.get(d, ()))))
    monkeypatch.setattr(svc, "_pending_pickups", lambda team_id: list(h.pending))
    monkeypatch.setattr(svc, "_open_lanes", lambda team_id: list(h.open_lanes))
    monkeypatch.setattr(svc, "_set_projections", lambda rows, now: h.projections.extend(rows))
    monkeypatch.setattr(svc, "_insert_lane", insert)
    monkeypatch.setattr(svc, "_record_decision", record)
    monkeypatch.setattr(svc, "_lane_step", step)
    monkeypatch.setattr(svc, "_lane_has_pending", lambda lane_id: h.lane_pending or lane_id in h.lane_pending_ids)
    monkeypatch.setattr(svc, "_adopt_executed", lambda lane_id, holder_id, on_board: h.adopted)
    monkeypatch.setattr(svc, "_lane_pickups", lambda lane_id: list(h.lane_pickups))
    monkeypatch.setattr(svc, "_cancel_pickup", cancel)
    monkeypatch.setattr(svc, "nba_ids_by_espn_id", lambda ids: {})
    monkeypatch.setattr(svc, "adds_for_day", day_adds)
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    return h


def decide(h, lane_row, now):
    """One lane's run: what its team's run tells the user about it."""
    notices = run(svc.StreamLaneService._decide_team([lane_row], now))
    assert len(notices) <= 1
    return notices[0] if notices else None


# ---- starting ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_start_stores_the_lane_its_projection_and_its_first_decision(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    resp = run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER, notify="desk")))
    fields, event = h.inserted[0]
    assert (fields["start_period"], fields["end_period"], fields["next_period"]) == (28, 34, 28)
    assert fields["notify"] == "desk" and fields["holder_player_id"] == HOLDER and event["action"] == "started"
    # Today's add still counts (before the tip); the holder plays today, the free agent tomorrow.
    days = fields["projection"]["days"]
    assert [d["player"]["player_id"] for d in days][:3] == [HOLDER, FA, FA]
    # The first decision is today's: its window (yesterday's tip) has passed, so it's due at once.
    assert fields["next_decision_at"] <= NOON
    assert resp.data.projection.gain > 0 and "Holder" in resp.message


@pytest.mark.unit
def test_after_todays_tip_the_lane_starts_tomorrow(h, monkeypatch):
    late = tip(MON) + timedelta(minutes=5)
    monkeypatch.setattr(svc, "datetime", _clock(late))
    run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    fields, _ = h.inserted[0]
    assert fields["start_period"] == fields["next_period"] == 29
    # Tuesday's decision is made when a pickup for it can first be made, at Monday's first tip-off: due at once.
    assert fields["next_decision_at"] == opens(TUE) <= late


@pytest.mark.unit
@pytest.mark.parametrize("arrange, error, reason", [
    (lambda h: setattr(h, "open_lanes", [lane(id=5, status="paused")]), svc.StreamLaneExists, None),
    (lambda h: setattr(h, "open_lanes", [lane(id=i, holder_player_id=100 + i) for i in range(svc.MAX_LANES)]),
     svc.StreamLaneLimit, None),
    (lambda h: h.pending.append(svc.PendingPickup(1, 30, MON, 99, HOLDER)), svc.StreamLaneInvalid, "scheduled_out"),
    (lambda h: setattr(h, "board", board(acq=None)), svc.StreamLaneInvalid, "no_matchup_end"),
    (lambda h: setattr(h, "board", board(acq=AcquisitionState(per="day", limit=1, matchup_end=MON, waiver_hours=24))),
     None, None),
])
def test_start_refuses(h, monkeypatch, arrange, error, reason):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    arrange(h)
    if error is None:     # a matchup that ends today, before the tip: today is still open
        run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
        assert h.inserted[0][0]["end_period"] == 28
        return
    with pytest.raises(error) as exc:
        run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    if reason:
        assert exc.value.data["reason"] == reason
    assert not h.inserted


@pytest.mark.unit
def test_the_spot_must_be_an_active_players(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    h.board = board([lineup_player(HOLDER, "Holder", IR, "MIA", eligible=(UT, IR))])
    for pid, reason in ((HOLDER, "on_ir"), (99, "not_on_roster")):
        with pytest.raises(svc.StreamLaneInvalid) as exc:
            run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=pid)))
        assert exc.value.data["reason"] == reason


@pytest.mark.unit
def test_a_matchup_with_no_open_day_left(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(tip(MON) + timedelta(hours=1)))
    h.board = board(acq=AcquisitionState(per="matchup", limit=4, matchup_end=MON, waiver_hours=24))
    with pytest.raises(svc.StreamLaneInvalid) as exc:
        run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=HOLDER)))
    assert exc.value.data["reason"] == "matchup_over"


@pytest.mark.unit
def test_writes_off_refuse_a_start_but_not_a_dry_run(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    with pytest.raises(svc.RosterWriteDisabled):
        run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    resp = run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=HOLDER)))
    assert resp.data.start_period == 28 and resp.data.first_decision_at == NOON


# ---- a decision -------------------------------------------------------------------------------


def tuesday(h):
    """The rollover into Tuesday: Tuesday's decision, due since Monday's first tip-off, is made now."""
    h.board = board(period=29, nba_date=TUE.isoformat())
    return svc.rollover_at(TUE)


@pytest.mark.unit
def test_a_switch_schedules_the_pickup_and_the_next_decision(h):
    """The holder sits Tuesday and the free agent plays it."""
    now = tuesday(h)
    notice = decide(h, lane(), now)
    assert notice is None
    rec = h.recorded[0]
    assert (rec.lane_id, rec.attempts) == (7, 3)
    pk = rec.pickup
    assert (pk["add_player_id"], pk["drop_player_id"], pk["scoring_period_id"], pk["lane_id"]) == (FA, HOLDER, 29, 7)
    assert pk["nba_date"] == TUE and pk["deadline_at"] == tip(TUE)
    assert pk["not_before_at"] <= now
    (event,) = rec.events
    assert event["action"] == "switch" and (event["add_player_id"], event["drop_player_id"]) == (FA, HOLDER)
    # The next decision is Wednesday's, made at Tuesday's first tip-off (whoever is dropped then plays Tuesday out).
    assert rec.fields["next_period"] == 30
    assert rec.fields["next_decision_at"] == opens(TUE + timedelta(days=1))
    assert rec.fields["projection"]["days"][0]["player"]["player_id"] == FA


@pytest.mark.unit
def test_a_keep_records_the_day_and_moves_on(h):
    h.pool = []
    decide(h, lane(), tuesday(h))
    rec = h.recorded[0]
    assert rec.pickup is None and [e["action"] for e in rec.events] == ["keep"]
    assert rec.fields["next_period"] == 30


@pytest.mark.unit
def test_the_decision_says_how_it_compares_with_the_projection(h):
    shown = {"days": [{"scoring_period_id": 29, "player": {"player_id": FA2, "name": "Gone", "team": "LAL"}}]}
    decide(h, lane(projection=shown), tuesday(h))
    event = h.recorded[0].events[0]
    assert (event["projected_player_id"], event["projected_name"], event["reason"]) == (FA2, "Gone", "projected_gone")


@pytest.mark.unit
def test_while_its_last_pickup_is_pending_the_decision_waits_without_reading(h):
    h.lane_pending = True
    now = tip(MON)
    decide(h, lane(), now)
    assert h.reads == 0 and not h.recorded
    assert h.steps[0].fields["next_decision_at"] == now + svc.PICKUP_WAIT and h.steps[0].attempts == 3


@pytest.mark.unit
def test_before_the_window_opens_the_decision_waits_for_it(h):
    """Run a minute before Monday's first tip-off for Tuesday: a pickup for Tuesday can't go before that tip."""
    now = tip(MON) - timedelta(minutes=1)
    decide(h, lane(next_period=29), now)
    assert not h.recorded and h.steps[0].fields["reason"] == "window_not_open"
    assert h.steps[0].fields["next_decision_at"] == opens(TUE)


@pytest.mark.unit
def test_a_provider_error_waits_and_an_expired_connection_pauses(h):
    h.board = RuntimeError("ESPN 503")
    decide(h, lane(), tip(MON))
    assert h.steps[0].fields["reason"] == "provider_error"
    h.board = ProviderAuthError("espn", "expired")
    notice = decide(h, lane(), tip(MON))
    assert h.recorded[-1].fields["status"] == "paused" and notice.action == "paused" and notice.reason == "auth_expired"


@pytest.mark.unit
def test_a_holder_who_left_the_roster_pauses_the_lane(h):
    h.board = board([lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE))])
    notice = decide(h, lane(), tip(MON))
    rec = h.recorded[0]
    assert rec.fields["status"] == "paused" and rec.events[-1]["action"] == "paused"
    assert (notice.action, notice.reason, notice.holder.player_id, notice.notify) == ("paused", "holder_gone", HOLDER, "email")


@pytest.mark.unit
def test_a_holder_replaced_by_the_lanes_own_pickup_is_adopted(h):
    """The settle that should have moved the holder didn't land: the lane's executed pickup says who it is."""
    h.board = board([lineup_player(FA2, "New", UT, "LAL", eligible=(UT, BE)),
                     lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE))])
    h.adopted = SimpleNamespace(add_player_id=FA2)
    decide(h, lane(), opens(TUE))
    rec = h.recorded[0]
    assert rec.fields["holder_player_id"] == FA2 and rec.fields["holder_name"] == "New"


@pytest.mark.unit
def test_days_that_passed_without_a_decision_are_told_and_the_lane_carries_on(h):
    """The tick was down from Monday morning to Tuesday 8 PM: Tuesday has gone, Wednesday is next."""
    tue = MON + timedelta(days=1)
    h.board = board(period=29, nba_date=tue.isoformat())
    now = tip(tue) + timedelta(hours=1)
    notice = decide(h, lane(next_period=29), now)
    rec = h.recorded[0]
    assert rec.events[0]["action"] == "blocked" and rec.events[0]["reason"] == "missed"
    assert (notice.action, notice.reason, notice.scoring_period_id) == ("blocked", "missed", 29)
    assert notice.ended is False
    assert rec.events[1]["scoring_period_id"] == 30 and rec.fields["next_period"] == 31


@pytest.mark.unit
def test_days_missed_up_to_the_matchups_end_end_the_lane_and_its_notice_says_so(h):
    """The tick was down through the matchup's last day: the word about the missed day says the lane is over."""
    tue = MON + timedelta(days=1)
    h.board = board(period=29, nba_date=tue.isoformat())
    notice = decide(h, lane(next_period=29, end_period=29), tip(tue) + timedelta(hours=1))
    rec = h.recorded[0]
    assert rec.fields["status"] == "ended" and rec.fields["reason"] == "matchup_over"
    assert (notice.action, notice.reason, notice.ended) == ("blocked", "missed", True)


@pytest.mark.unit
def test_after_missed_days_the_next_decision_still_waits_for_its_window(h):
    """As above, but the holder played Tuesday: Wednesday's window (Tuesday's first tip-off) has opened all the
    same, so the decision is made now and its pickup is due now — never one waiting hours on an early choice."""
    h.schedule[TUE] = {"LAL", "MIA"}
    h.board = board(period=29, nba_date=TUE.isoformat())
    now = tip(TUE) + timedelta(hours=1)
    notice = decide(h, lane(next_period=29), now)
    rec = h.recorded[0]
    assert (rec.events[0]["action"], rec.events[0]["reason"], notice.reason) == ("blocked", "missed", "missed")
    assert rec.events[1]["scoring_period_id"] == 30 and rec.fields["next_period"] == 31
    assert rec.pickup is None or rec.pickup["not_before_at"] <= now


@pytest.mark.unit
def test_the_last_days_decision_ends_the_lane_at_its_tip(h):
    sat = MON + timedelta(days=5)
    h.board = board(period=32, nba_date=(sat - timedelta(days=1)).isoformat())
    decide(h, lane(next_period=33, end_period=33, next_decision_at=tip(sat - timedelta(days=1))),
           tip(sat - timedelta(days=1)))
    rec = h.recorded[0]
    assert rec.fields["next_period"] == 34 and rec.fields["next_decision_at"] == tip(sat)
    h.recorded.clear()
    reads = h.reads
    decide(h, lane(next_period=34, end_period=33), tip(sat))
    rec = h.recorded[0]
    assert rec.fields["status"] == "ended" and rec.fields["reason"] == "matchup_over" and h.reads == reads


@pytest.mark.unit
def test_a_lane_whose_team_is_gone_ends(h):
    def gone(team_id, user_id):
        raise NotFoundError("TEAM_NOT_FOUND", "gone")
    svc_load = svc._load_team_for_job
    try:
        svc._load_team_for_job = gone
        assert decide(h, lane(), tip(MON)) is None
    finally:
        svc._load_team_for_job = svc_load
    assert h.recorded[0].fields["status"] == "ended" and h.recorded[0].fields["reason"] == "team_not_found"


@pytest.mark.unit
def test_a_decision_that_lost_its_claim_tells_nobody(h):
    h.held = False
    h.board = board([lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE))])
    assert decide(h, lane(), tip(MON)) is None


@pytest.mark.unit
def test_the_tick_carries_on_past_a_broken_team(h, monkeypatch):
    claimed = [lane(id=1, team_id=22), lane(id=2)]
    monkeypatch.setattr(svc, "_claim_due_lanes", lambda now, limit: claimed)
    h.board = board([lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE))])
    real = svc.StreamLaneService._decide_team

    async def boom_then_real(lanes, now):
        if lanes[0].team_id == 22:
            raise RuntimeError("bad row")
        return await real(lanes, now)
    monkeypatch.setattr(svc.StreamLaneService, "_decide_team", staticmethod(boom_then_real))
    notices = run(svc.StreamLaneService.decide_due(tip(MON), 4))
    assert h.steps[0].lane_id == 1 and h.steps[0].fields["reason"] == "error"
    assert [n.lane_id for n in notices] == [2]           # lane 2's holder is gone: paused, and told


# ---- a team's lanes, together -------------------------------------------------------------------


def two_spots(h, *, used=3):
    """The holder (UT) and a big (C), both MIA, both streamed; the league leaves 4 - `used` adds. Two LAL
    free agents play Tuesday and Thursday: one fills UT, the other C."""
    h.board = board([lineup_player(HOLDER, "Holder", UT, "MIA", eligible=(UT, BE), avg=15),
                     lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE), avg=30),
                     lineup_player(BIG, "Big", C, "MIA", eligible=(C, UT, BE), avg=15)],
                    period=29, nba_date=TUE.isoformat(),
                    acq=AcquisitionState(per="matchup", limit=4, matchup_period_id=5, matchup_start=MON,
                                         matchup_end=date(2026, 11, 22), matchup_used=used, waiver_hours=24))
    h.board = h.board.model_copy(update={"slot_counts": {"0": 1, "4": 1, "11": 1, "12": 3, "13": 1}})
    h.pool = [free_agent(FA, "Free Agent", "LAL"), free_agent(FA2, "Free Big", "LAL", positions=("C", "UT1"), pos=5)]
    return svc.rollover_at(TUE), [lane(), lane(id=8, holder_player_id=BIG, holder_name="Big", original_player_id=BIG,
                                           original_name="Big")]


@pytest.mark.unit
def test_lanes_deciding_a_day_share_the_adds_left(h):
    """One add for two spots that would each take a free agent: the plan spends it once."""
    now, lanes = two_spots(h, used=3)
    assert run(svc.StreamLaneService._decide_team(lanes, now)) == []
    assert [r.lane_id for r in h.recorded] == [7, 8]
    pickups = [r.pickup for r in h.recorded if r.pickup]
    assert len(pickups) == 1
    keep = next(r for r in h.recorded if r.pickup is None)
    assert [e["action"] for e in keep.events] == ["keep"]
    for r in h.recorded:
        proj = r.fields["projection"]
        assert proj["lanes"] == 2 and proj["adds_left"] == 1 and r.fields["next_period"] == 30
    assert sum(r.fields["projection"]["adds"] for r in h.recorded) == 1


@pytest.mark.unit
def test_lanes_with_adds_to_spare_each_take_a_different_free_agent(h):
    now, lanes = two_spots(h, used=2)
    run(svc.StreamLaneService._decide_team(lanes, now))
    pickups = {r.lane_id: r.pickup for r in h.recorded}
    assert (pickups[7]["drop_player_id"], pickups[8]["drop_player_id"]) == (HOLDER, BIG)
    assert {pickups[7]["add_player_id"], pickups[8]["add_player_id"]} == {FA, FA2}
    assert all(p["lane_id"] == lane_id and p["scoring_period_id"] == 29 for lane_id, p in pickups.items())


@pytest.mark.unit
def test_a_lane_waiting_on_its_pickup_leaves_the_others_to_decide(h):
    now, lanes = two_spots(h, used=2)
    h.lane_pending_ids = {8}
    run(svc.StreamLaneService._decide_team(lanes, now))
    assert [s.lane_id for s in h.steps] == [8] and h.steps[0].fields["reason"] == "pickup_pending"
    (rec,) = h.recorded
    assert rec.lane_id == 7 and rec.fields["projection"]["lanes"] == 1


@pytest.mark.unit
def test_a_day_that_fails_leaves_the_teams_other_days_to_decide(h, monkeypatch):
    now, (first, second) = two_spots(h, used=2)
    second.next_period = 30                              # decides Wednesday: a group of its own
    real = svc.StreamLaneService._decide_together

    async def boom_then_real(group, period, *args):
        if period == 29:
            raise RuntimeError("bad day")
        return await real(group, period, *args)
    monkeypatch.setattr(svc.StreamLaneService, "_decide_together", staticmethod(boom_then_real))
    assert run(svc.StreamLaneService._decide_team([first, second], now)) == []
    assert [(s.lane_id, s.fields["reason"]) for s in h.steps] == [(7, "error"), (8, "window_not_open")]


@pytest.mark.unit
def test_a_lane_started_beside_one_deciding_the_same_day_is_planned_with_it(h, monkeypatch):
    now, _ = two_spots(h, used=2)
    monkeypatch.setattr(svc, "datetime", _clock(now))
    other = lane(id=5, holder_player_id=BIG, holder_name="Big", next_period=29, attempts=2)
    h.open_lanes = [other]
    plan = run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=HOLDER))).data
    assert plan.projection.lanes == 2 and [o.lane_id for o in plan.others] == [5]
    assert plan.others[0].holder.player_id == BIG and plan.others[0].projection.lanes == 2
    run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    fields, _ = h.inserted[0]
    assert fields["projection"]["lanes"] == 2
    ((lane_id, attempts, projection),) = h.projections
    assert (lane_id, attempts, projection["lanes"]) == (5, 2, 2)


@pytest.mark.unit
def test_a_lane_that_has_decided_the_day_is_roster_for_a_new_one(h, monkeypatch):
    now, _ = two_spots(h, used=2)
    monkeypatch.setattr(svc, "datetime", _clock(now))
    h.open_lanes = [lane(id=5, holder_player_id=BIG, holder_name="Big", next_period=30)]
    run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    assert h.inserted[0][0]["projection"]["lanes"] == 1 and h.projections == []


@pytest.mark.unit
def test_a_spot_streamed_already_or_a_full_team_refuses_the_dry_run_too(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    h.open_lanes = [lane(id=5)]
    with pytest.raises(svc.StreamLaneExists):
        run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=HOLDER)))
    h.open_lanes = [lane(id=i, holder_player_id=100 + i) for i in range(svc.MAX_LANES)]
    with pytest.raises(svc.StreamLaneLimit):
        run(svc.StreamLaneService.plan(TEAM, LEAGUE, StreamLanePlanReq(player_id=HOLDER)))


@pytest.mark.unit
def test_a_league_with_longer_waivers_can_stream(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    h.board = board(acq=AcquisitionState(per="matchup", limit=4, matchup_end=date(2026, 11, 22), waiver_hours=48))
    run(svc.StreamLaneService.start(TEAM, LEAGUE, StreamLaneStartReq(player_id=HOLDER)))
    assert h.inserted[0][0]["projection"]["rehold_after"] == 3


# ---- the user's changes --------------------------------------------------------------------------


def _team_lane(h, monkeypatch, row):
    monkeypatch.setattr(svc, "_team_lane", lambda team_id, lane_id: row if lane_id == row.id else None)


@pytest.mark.unit
def test_pause_and_notify(h, monkeypatch):
    _team_lane(h, monkeypatch, lane(reason=None))
    run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="paused", notify="desk")))
    step = h.steps[0]
    assert step.attempts is None and step.fields["status"] == "paused" and step.fields["notify"] == "desk"
    assert step.event["action"] == "paused"
    with pytest.raises(NotFoundError):
        run(svc.StreamLaneService.update(TEAM, LEAGUE, 8, StreamLaneUpdateReq(status="paused")))


@pytest.mark.unit
def test_pause_cancels_the_lanes_pickup_and_its_day_is_decided_again_on_resume(h, monkeypatch):
    """Tuesday's switch is scheduled (the lane has moved on to Wednesday): pausing takes it back off ESPN's way."""
    _team_lane(h, monkeypatch, lane(reason=None, next_period=30))
    h.lane_pickups = [SimpleNamespace(id=501, scoring_period_id=29)]
    run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="paused")))
    # Before the pause lands, and once more after it, for a decision that landed in between.
    assert h.cancelled == [501, 501]
    step = h.steps[0]
    assert step.fields["status"] == "paused" and step.fields["next_period"] == 29
    assert step.event["scoring_period_id"] == 29


@pytest.mark.unit
def test_pause_waits_while_the_lanes_pickup_is_being_made(h, monkeypatch):
    _team_lane(h, monkeypatch, lane(reason=None, next_period=30))
    h.lane_pickups = [SimpleNamespace(id=501, scoring_period_id=29)]
    h.cancel_prior = "in_progress"
    with pytest.raises(svc.StreamLaneBusy):
        run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="paused")))
    assert not h.steps


@pytest.mark.unit
def test_resume_needs_the_holder_and_picks_up_from_the_next_open_day(h, monkeypatch):
    monkeypatch.setattr(svc, "datetime", _clock(tip(MON) + timedelta(hours=2)))
    _team_lane(h, monkeypatch, lane(status="paused", next_period=28, next_decision_at=None))
    run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="active")))
    step = h.steps[0]
    assert step.fields["status"] == "active" and step.fields["next_period"] == 29 and step.event["action"] == "resumed"
    h.board = board([lineup_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE))])
    with pytest.raises(svc.StreamLaneInvalid) as exc:
        run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="active")))
    assert exc.value.data["reason"] == "holder_gone"


@pytest.mark.unit
def test_resume_refuses_while_a_pickup_scheduled_during_the_pause_drops_the_holder(h, monkeypatch):
    """Paused, the lane's holder is the user's to drop; resuming over that pickup would have two plans for the spot."""
    monkeypatch.setattr(svc, "datetime", _clock(NOON))
    _team_lane(h, monkeypatch, lane(status="paused", next_period=28, next_decision_at=None))
    h.pending.append(svc.PendingPickup(1, 30, MON + timedelta(days=2), 99, HOLDER))
    with pytest.raises(svc.StreamLaneInvalid) as exc:
        run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="active")))
    assert exc.value.data["reason"] == "scheduled_out" and not h.steps
    # The lane's own pickup (one a pause could not cancel) is not in its way.
    h.pending[0] = svc.PendingPickup(1, 30, MON + timedelta(days=2), 99, HOLDER, lane_id=7)
    run(svc.StreamLaneService.update(TEAM, LEAGUE, 7, StreamLaneUpdateReq(status="active")))
    assert h.steps[0].fields["status"] == "active"


@pytest.mark.unit
def test_stop_cancels_the_lanes_pickup_unless_it_is_being_made(h, monkeypatch):
    _team_lane(h, monkeypatch, lane())
    h.lane_pickups = [SimpleNamespace(id=501, scoring_period_id=28)]
    run(svc.StreamLaneService.stop(TEAM, 7))
    assert h.cancelled[0] == 501 and h.steps[0].fields["status"] == "ended" and h.steps[0].fields["reason"] == "stopped"
    h.steps.clear()
    h.cancel_prior = "in_progress"
    with pytest.raises(svc.StreamLaneBusy):
        run(svc.StreamLaneService.stop(TEAM, 7))
    assert not h.steps
    _team_lane(h, monkeypatch, lane(status="ended"))
    with pytest.raises(svc.StreamLaneEnded):
        run(svc.StreamLaneService.stop(TEAM, 7))


# ---- a settled lane pickup ------------------------------------------------------------------------


@pytest.mark.unit
def test_a_settled_pickup_tells_the_lane_and_its_email(h, monkeypatch):
    seen = []

    def noted(lane_id, pickup_id, status, reason, detail, add, drop, period, nba_date, now):
        seen.append((lane_id, pickup_id, status, add, drop))
        return {"lane_id": lane_id, "notify": "email", "projected": (FA2, "Projected"), "why": "replanned",
                "last_day": period == 33}
    monkeypatch.setattr(svc, "_note_pickup_settled", noted)
    row = SimpleNamespace(id=501, lane_id=7, add_player_id=FA, add_name="Free Agent", add_team="LAL",
                          drop_player_id=HOLDER, drop_name="Holder", scoring_period_id=29, nba_date=MON)
    note = run(svc.StreamLaneService.note_pickup_settled(row, "executed", None, None, NOON))
    assert seen == [(7, 501, "executed", (FA, "Free Agent", "LAL"), (HOLDER, "Holder"))]
    assert (note.lane_id, note.notify, note.why, note.projected.name) == (7, "email", "replanned", "Projected")
    assert note.last_day is False
    row.scoring_period_id = 33
    assert run(svc.StreamLaneService.note_pickup_settled(row, "executed", None, None, NOON)).last_day is True

    def broken(*args):
        raise RuntimeError("db down")
    monkeypatch.setattr(svc, "_note_pickup_settled", broken)
    assert run(svc.StreamLaneService.note_pickup_settled(row, "executed", None, None, NOON)) is None


def _clock(now):
    """`datetime` with a fixed now(), for the routes that read the clock."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)
    return Clock
