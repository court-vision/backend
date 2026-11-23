"""
services.daily_actions_job (Daily actions at Act) without ESPN, the writer or Postgres:
each read of the Week desk's list is a prebuilt DailyActionsData from a queue, the two
write chains are fakes that record what they were sent, and the team's level, its
pending pickups and the team itself are plain stubs.

A small roster (PG, UT, two bench, one IR): the pass makes the list's IR moves and
fills open spots, never a drop, and stops where the league's adds, a scheduled pickup
or ESPN say so.
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.daily_actions import (
    DailyAction,
    DailyActionPlayer,
    DailyActionsData,
    DailyActionsResp,
    DailyActionsRunReq,
    DailyActionTransaction,
)
from schemas.lineup_editor import AcquisitionState, LineupMoveResult, LineupPlayer, LineupState
from schemas.streamer import StreamerPlayerResp
from services import daily_actions_job as job
from services.lineup_editor_service import RosterWriteRejected, RosterWriteUnavailable
from services.roster_transaction_service import RosterTransactionInvalid

PG, UT, BE, IR = 0, 11, 12, 13
COUNTS = {"0": 1, "11": 1, "12": 2, "13": 1}         # four seats off IR, one IR seat
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="Test", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=3)
TEAM = SimpleNamespace(team_id=22, user_id=7, league=None)
DAY = date(2026, 11, 24)
MORNING = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)   # noon ET, the tip is 7:30 PM ET


def player(pid, slot, *, status=None):
    return LineupPlayer(
        player_id=pid, nba_player_id=pid * 10, name=f"P{pid}", team="NYK", lineup_slot_id=slot,
        lineup_slot=str(slot), eligible_slot_ids=[PG, UT, BE, IR], eligible_slots=["PG", "UT", "BE", "IR"],
        injured=status == "OUT", injury_status=status, has_game_today=True, game_time_et="19:30",
        playable=status != "OUT", avg_points=20.0,
    )


def board(players, *, nba_date="2026-11-24", tip="19:30", acquisitions=None):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="Test", espn_team_id=3, nba_date=nba_date, scoring_period_id=35,
        scoring_period_source="provider", first_game_time_et=tip, slot_counts=COUNTS, players=players,
        can_write=True, roster_version="v1", fetched_at="now", acquisitions=acquisitions,
    )


def row_player(pid, slot=None):
    return DailyActionPlayer(player_id=pid, nba_player_id=pid * 10, name=f"P{pid}", team="NYK", lineup_slot_id=slot)


def ir_in(pid, slot=BE):
    return DailyAction(id=f"ir_in:{pid}", kind="ir_in", title=f"Move P{pid} to IR", player=row_player(pid, slot),
                       moves=[LineupMoveResult(player_id=pid, name=f"P{pid}", from_slot_id=slot, from_slot=str(slot),
                                               to_slot_id=IR, to_slot="IR", role="shift", note="OUT")])


def ir_out_blocked(pid):
    return DailyAction(id=f"ir_out:{pid}", kind="ir_out", title=f"Move P{pid} off IR", player=row_player(pid, IR),
                       blocked_reason="roster_full")


def add(pid):
    pickup = StreamerPlayerResp(
        player_id=pid, nba_player_id=pid * 10, name=f"FA{pid}", team="LAL", valid_positions=["PG"],
        avg_points_last_n=25.0, avg_points_season=25.0, avg_source="rolling", games_remaining=3, has_b2b=False,
        b2b_game_count=0, game_days=[0], streamer_score=50.0, injured=False, acquisition_status="free_agent",
    )
    return DailyAction(id=f"add:{pid}", kind="add", title=f"Pick up FA{pid}",
                       detail=f"Open roster seat · FA{pid} plays tonight vs BOS · 7:30 PM · 25.0 avg",
                       player=DailyActionPlayer(player_id=pid, name=f"FA{pid}", team="LAL"),
                       transaction=DailyActionTransaction(pickup=pickup, drop_player_id=None))


def swap(pid, drop):
    a = add(pid)
    return a.model_copy(update={"id": f"add_drop:{pid}:{drop}", "kind": "add_drop",
                                "transaction": DailyActionTransaction(pickup=a.transaction.pickup,
                                                                      drop_player_id=drop)})


FULL = [player(1, PG), player(2, UT), player(3, BE), player(4, BE)]


def listing(players, actions, **kw):
    return DailyActionsData(lineup=board(players, **kw), can_write=True, nba_date=kw.get("nba_date", "2026-11-24"),
                            actions=actions)


@pytest.fixture
def h(monkeypatch):
    h = SimpleNamespace(level=("act", False), team=(TEAM, LEAGUE), pending=job.Pending(), reads=[],
                        lineup_writes=[], txn_writes=[], validate=[], lineup_raise=None, txn_raise=None,
                        now=MORNING, clock_after={})

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def fake_read(team, league_info, **kw):
        nxt = h.reads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return DailyActionsResp(status=ApiStatus.SUCCESS, message="", data=nxt)

    async def fake_team(team_id, user_id):
        return h.team

    async def lineup_write(**kw):
        if h.lineup_raise:
            raise h.lineup_raise
        h.lineup_writes.append((kw["source"], [(m.player_id, m.from_slot_id, m.to_slot_id) for m in kw["moves"]]))
        h.now = h.clock_after.get("lineup_write", h.now)
        return kw["state"], True, 100 + len(h.lineup_writes)

    async def validate(league_info, state, add_id, drop, **kw):
        h.validate.append((add_id, drop))
        h.now = h.clock_after.get("validate", h.now)
        nxt = h.txn_raise if isinstance(h.txn_raise, RosterTransactionInvalid) else None
        if nxt:
            raise nxt
        return None, SimpleNamespace(name=f"FA{add_id}", pro_team="LAL")

    async def txn_write(**kw):
        if h.txn_raise and not isinstance(h.txn_raise, RosterTransactionInvalid):
            raise h.txn_raise
        h.txn_writes.append((kw["source"], kw["add"], kw["drop"], kw["moves"]))
        return kw["state"], True, 200 + len(h.txn_writes)

    monkeypatch.setattr(job, "run_db", direct_run_db)
    async def level_of(team_id, user_id):
        return h.level

    monkeypatch.setattr(job, "_level_of", level_of)
    monkeypatch.setattr(job, "_pending", lambda team_id: h.pending)
    monkeypatch.setattr(job, "_team", fake_team)
    monkeypatch.setattr(job, "_now", lambda: h.now)
    monkeypatch.setattr(job.DailyActionsService, "read", staticmethod(fake_read))
    monkeypatch.setattr(job.LineupEditorService, "_write", staticmethod(lineup_write))
    monkeypatch.setattr(job.RosterTransactionService, "_validate", staticmethod(validate))
    monkeypatch.setattr(job.RosterTransactionService, "_write", staticmethod(txn_write))
    monkeypatch.setattr(job.settings, "roster_writes_enabled", True)
    return h


def run():
    return asyncio.run(job.DailyActionsJob.run(DailyActionsRunReq(team_id=22, user_id=7, nba_date=DAY))).data


@pytest.mark.unit
def test_an_injured_player_goes_to_ir_and_the_spot_he_frees_is_filled(h):
    hurt = player(4, BE, status="OUT")
    h.reads = [
        listing([*FULL[:3], hurt], [ir_in(4)]),
        listing([*FULL[:3], player(4, IR, status="OUT")], [add(90)]),
        listing([*FULL[:3], player(4, IR, status="OUT"), player(90, BE)], []),
    ]
    out = run()

    assert out.outcome == "applied" and out.held is None
    assert h.lineup_writes == [("daily_actions", [(4, BE, IR)])]
    assert [(s, a, d) for s, a, d, _ in h.txn_writes] == [("daily_actions", 90, None)]
    assert h.txn_writes[0][3] == [{"player_id": 90, "action": "add", "name": "FA90"}]
    assert [m.player_id for m in out.ir_moves] == [4]
    assert [(a.player.name, a.verified, a.audit_id) for a in out.adds] == [("FA90", True, 201)]
    assert "plays tonight" in out.adds[0].detail
    assert (out.open_spots, out.nba_date, out.first_game_time_et, out.team_name) == (0, "2026-11-24", "19:30", "Test")


@pytest.mark.unit
def test_it_never_drops_anyone(h):
    h.reads = [listing(FULL, [swap(90, 3), ir_out_blocked(5)])]
    out = run()

    assert (out.outcome, out.ir_moves, out.adds) == ("noop", [], [])
    assert h.lineup_writes == [] and h.txn_writes == [] and h.validate == []


@pytest.mark.unit
def test_every_open_spot_is_filled_one_read_at_a_time(h):
    two_open = FULL[:2]
    h.reads = [
        listing(two_open, [add(90)]),
        listing([*two_open, player(90, BE)], [add(91)]),
        listing([*two_open, player(90, BE), player(91, BE)], []),
    ]
    out = run()

    assert [a for _, a, _, _ in h.txn_writes] == [90, 91]
    assert out.outcome == "applied" and out.held is None and out.open_spots == 0


@pytest.mark.unit
def test_a_spot_a_scheduled_add_only_pickup_needs_stays_open(h):
    h.pending = job.Pending(days=(date(2026, 11, 26),), add_only=(("Toumani Camara", date(2026, 11, 26)),))
    h.reads = [listing(FULL[:3], [add(90)])]
    out = run()

    assert h.txn_writes == []
    assert (out.outcome, out.open_spots) == ("noop", 1)
    assert out.held == "The open spot is kept for the pickup you scheduled: Toumani Camara on Thu Nov 26"


@pytest.mark.unit
def test_a_second_open_spot_is_filled_beside_a_scheduled_pickup(h):
    h.pending = job.Pending(days=(date(2026, 11, 26),), add_only=(("Toumani Camara", date(2026, 11, 26)),))
    h.reads = [listing(FULL[:2], [add(90)]), listing([*FULL[:2], player(90, BE)], [add(91)])]
    out = run()

    assert [a for _, a, _, _ in h.txn_writes] == [90]
    assert out.outcome == "applied" and out.held.startswith("The open spot is kept for the pickup you scheduled")


@pytest.mark.unit
def test_the_leagues_add_limit_keeps_a_spot_open(h):
    used = AcquisitionState(per="matchup", limit=2, matchup_used=2, matchup_start=date(2026, 11, 23),
                            matchup_end=date(2026, 11, 29))
    h.reads = [listing(FULL[:3], [add(90)], acquisitions=used)]
    out = run()

    assert h.txn_writes == [] and out.outcome == "noop"
    assert out.held == "An open spot stays open: Every add this matchup allows is used (2 of 2)"


@pytest.mark.unit
@pytest.mark.parametrize("level, reason", [(("off", False), "not_act"), (("act", True), "at_ai")])
def test_a_team_not_at_act_or_at_ai_is_left_alone(h, level, reason):
    h.level = level
    out = run()
    assert (out.outcome, out.reason) == ("skipped", reason)
    assert h.reads == [] and h.lineup_writes == [] and h.txn_writes == []


@pytest.mark.unit
def test_writes_switched_off_skip_every_team(h, monkeypatch):
    monkeypatch.setattr(job.settings, "roster_writes_enabled", False)
    out = run()
    assert (out.outcome, out.reason) == ("skipped", "writes_disabled")


@pytest.mark.unit
def test_after_the_first_tip_nothing_is_sent(h):
    h.reads = [listing(FULL[:3], [add(90)], tip="11:30")]
    out = run()
    assert (out.outcome, out.reason) == ("skipped", "after_first_tip") and h.txn_writes == []


TIP_PASSED = datetime(2026, 11, 25, 0, 31, tzinfo=timezone.utc)   # 7:31 PM ET, a minute past the tip


@pytest.mark.unit
def test_a_pass_that_runs_into_the_tip_keeps_its_ir_move_and_sends_no_pickup(h):
    h.clock_after = {"lineup_write": TIP_PASSED}
    h.reads = [listing([*FULL[:3], player(4, BE, status="OUT")], [ir_in(4)]),
               listing([*FULL[:3], player(4, IR, status="OUT")], [add(90)])]
    out = run()
    assert out.outcome == "applied" and [m.player_id for m in out.ir_moves] == [4]
    assert out.adds == [] and h.validate == [] and h.txn_writes == []
    assert out.held == job.TIPPED_HELD


@pytest.mark.unit
def test_a_pickup_whose_check_runs_into_the_tip_is_not_sent(h):
    h.clock_after = {"validate": TIP_PASSED}
    h.reads = [listing(FULL[:3], [add(90)])]
    out = run()
    assert h.validate == [(90, None)] and h.txn_writes == []
    assert (out.outcome, out.adds, out.held) == ("noop", [], job.TIPPED_HELD)


@pytest.mark.unit
def test_another_espn_day_is_not_todays(h):
    h.reads = [listing(FULL[:3], [add(90)], nba_date="2026-11-25")]
    out = run()
    assert (out.outcome, out.reason) == ("skipped", "date_mismatch") and h.txn_writes == []


@pytest.mark.unit
def test_espn_refusing_the_first_write_sends_nothing_else(h):
    h.lineup_raise = RosterWriteRejected(message="Roster lineup is invalid")
    h.reads = [listing([*FULL[:3], player(4, BE, status="OUT")], [ir_in(4)])]
    out = run()
    assert (out.outcome, out.reason) == ("rejected", "Roster lineup is invalid")
    assert out.ir_moves == [] and h.txn_writes == []


@pytest.mark.unit
def test_a_refused_pickup_after_an_ir_move_keeps_the_ir_move(h):
    h.txn_raise = RosterWriteUnavailable()
    h.reads = [listing([*FULL[:3], player(4, BE, status="OUT")], [ir_in(4)]),
               listing([*FULL[:3], player(4, IR, status="OUT")], [add(90)])]
    out = run()
    assert out.outcome == "applied" and [m.player_id for m in out.ir_moves] == [4] and out.adds == []
    assert out.held == "ESPN couldn't be reached"


@pytest.mark.unit
def test_a_pickup_espn_no_longer_lists_as_free_is_not_sent(h):
    h.txn_raise = RosterTransactionInvalid(message="FA90 is on another team's roster",
                                           data={"reason": "add_not_available", "player_id": 90})
    h.reads = [listing(FULL[:3], [add(90)])]
    out = run()
    assert (out.outcome, out.reason, out.held) == ("skipped", "add_not_available", "FA90 is on another team's roster")
    assert h.txn_writes == []


@pytest.mark.unit
def test_a_move_espn_doesnt_show_yet_is_never_sent_twice(h):
    stuck = listing(FULL[:3], [add(90)])
    h.reads = [stuck, stuck]
    out = run()
    assert [a for _, a, _, _ in h.txn_writes] == [90]
    assert out.outcome == "applied" and out.held == "ESPN didn't show the last move yet, so nothing more was sent"


@pytest.mark.unit
def test_the_pass_stops_at_its_write_cap(h):
    reads = []
    for i in range(job.MAX_WRITES + 1):
        reads.append(listing(FULL[:1], [add(90 + i)]))
    h.reads = reads
    out = run()
    assert len(h.txn_writes) == job.MAX_WRITES
    assert out.held == "An open spot stays open: the pass made as many moves as it makes in a day"


@pytest.mark.unit
def test_an_unreadable_board_is_a_failure_the_next_poll_retries(h):
    h.reads = [RuntimeError("ESPN timed out")]
    out = run()
    assert (out.outcome, out.reason) == ("failed", "read_failed")


def unread(data, error="ESPN timed out"):
    return data.model_copy(update={"streamers_error": error})


@pytest.mark.unit
def test_an_unread_free_agent_pool_with_a_spot_open_is_a_failure_the_next_poll_retries(h):
    # The pool's fetch failed, so the list has no pickup: that's no reason to settle the day as noop
    h.reads = [unread(listing(FULL[:3], []))]
    out = run()
    assert (out.outcome, out.reason, out.held) == ("failed", "pool_unread", job.POOL_HELD)
    assert h.txn_writes == [] and out.open_spots == 1


@pytest.mark.unit
def test_an_unread_pool_after_an_ir_move_keeps_the_move_and_says_the_spot_stayed_open(h):
    h.reads = [listing([*FULL[:3], player(4, BE, status="OUT")], [ir_in(4)]),
               unread(listing([*FULL[:2], player(4, IR, status="OUT")], []))]
    out = run()
    assert out.outcome == "applied" and [m.player_id for m in out.ir_moves] == [4]
    assert out.adds == [] and out.held == job.POOL_HELD


@pytest.mark.unit
@pytest.mark.parametrize("players, pending, error", [
    (FULL, job.Pending(), "ESPN timed out"),                                    # no spot open: nothing to pick
    (FULL[:3], job.Pending(), "value_kind_mismatch"),                           # a pool that never fits
    (FULL[:3], job.Pending(days=(date(2026, 11, 26),), add_only=(("Toumani Camara", date(2026, 11, 26)),)),
     "ESPN timed out"),                                                         # the spot is kept anyway
])
def test_an_unread_pool_is_no_failure_when_no_pickup_could_go(h, players, pending, error):
    h.pending = pending
    h.reads = [unread(listing(players, []), error)]
    out = run()
    assert out.outcome == "noop" and h.txn_writes == []


@pytest.mark.unit
def test_next_step_puts_ir_before_the_pickup():
    data = listing(FULL[:3], [add(90), ir_in(3)])
    step = job.next_step(data, job.Pending())
    assert step.kind == "ir" and [a.id for a in step.rows] == ["ir_in:3"]
