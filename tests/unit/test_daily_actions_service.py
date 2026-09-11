"""
services.daily_actions_service without ESPN or Postgres: the board read is a
prebuilt LineupState, the free-agent search a prebuilt StreamerResp (or an
exception), the games lookup and the calendar plain fakes.

Covers the non-ESPN answer, the fill plan grouped into chains, the IR rows, the
daily streamer swap and every guard around it (day, value kind, ratio, who may
be dropped), row order, copy and ids.
"""

import asyncio
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.streamer import StreamerData, StreamerMode, StreamerPlayerResp, StreamerResp
from services import daily_actions_service as svc
from services.lineup_editor_service import _slot_name

PG, SG, C, UT, BE, IR = 0, 1, 4, 11, 12, 13
COUNTS = {"0": 1, "1": 1, "4": 1, "11": 1, "12": 2, "13": 1}   # seven seats, IR included
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
YAHOO = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="Y", year=2027)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
OPENING = date(2026, 10, 20)   # a Tuesday


def player(pid, name, slot, *, eligible=(PG, SG, UT, BE, IR), game=True, status=None, injured=False,
           locked=False, value=20.0, team="DEN", tip="19:30", pos=None):
    return LineupPlayer(
        player_id=pid, nba_player_id=pid * 10, name=name, team=team, lineup_slot_id=slot, lineup_slot=_slot_name(slot),
        eligible_slot_ids=list(eligible), eligible_slots=[_slot_name(s) for s in eligible], injured=injured,
        injury_status=status, lineup_locked=locked, has_game_today=game, opponent="vs LAL" if game else None,
        game_time_et=tip if game else None, game_started=False, locked=locked,
        playable=game and (status or "ACTIVE").upper() != "OUT", avg_points=value, value_kind="fpts",
        value_source="rolling", default_position_id=pos,
    )


def full_board(overrides=None):
    """Seven players for seven seats: nobody can be added without a drop."""
    base = {
        1: player(1, "P1", PG), 2: player(2, "P2", SG), 5: player(5, "P5", C, eligible=(C, UT, BE, IR)),
        8: player(8, "P8", UT), 11: player(11, "P11", BE), 12: player(12, "P12", BE),
        14: player(14, "P14", IR, status="OUT"),
    }
    base.update(overrides or {})
    return list(base.values())


def state(players, *, nba_date="2026-10-20", can_write=True, reason=None, version="v1", limits=None):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
        scoring_period_id=1, scoring_period_source="provider", first_game_time_et="19:00", slot_counts=COUNTS,
        slots=[], lock_type="INDIVIDUAL_GAME", players=players, can_write=can_write, write_blocked_reason=reason,
        roster_version=version, fetched_at="now", position_limits=limits or {},
    )


def pick(pid, name="FA", *, status="free_agent", value=20.0, team="LAL", score=50.0, game_days=(0, 3), pos=None):
    return StreamerPlayerResp(
        player_id=pid, nba_player_id=pid * 10, name=name, team=team, valid_positions=["PG"],
        avg_points_last_n=value, avg_points_season=value or 0.0, avg_source="rolling", games_remaining=len(game_days),
        has_b2b=False, b2b_game_count=0, game_days=list(game_days), streamer_score=score, injured=False,
        acquisition_status=status, default_position_id=pos,
    )


def pool(*streamers, start=OPENING, target_day=0, value_kind="fpts"):
    return StreamerResp(status=ApiStatus.SUCCESS, message="ok", data=StreamerData(
        matchup_number=1, current_day_index=target_day, game_span=6, start_date=start,
        end_date=start + timedelta(days=5), upcoming=True, avg_days=7, mode=StreamerMode.DAILY,
        target_day=target_day, teams_with_b2b=[], streamers=list(streamers), value_kind=value_kind,
    ))


NO_POOL = StreamerResp(status=ApiStatus.SUCCESS, message="No matchup on the calendar", data=None)
LAL_HOME = SimpleNamespace(home_team_id="LAL", away_team_id="BOS", start_time_et=time(19, 30))


@pytest.fixture
def harness(monkeypatch):
    h = SimpleNamespace(board=None, pool=NO_POOL, find_calls=[], games=[], game_days={})

    async def fake_read(team_id, league_info, **kwargs):
        if isinstance(h.board, Exception):
            raise h.board
        return h.board

    async def fake_find(league_info, **kwargs):
        h.find_calls.append(kwargs)
        if isinstance(h.pool, Exception):
            raise h.pool
        return h.pool

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(svc.LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(svc.StreamerService, "find_streamers", staticmethod(fake_find))
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_games_on", lambda nba_date: h.games)
    monkeypatch.setattr(svc.schedule_service, "get_remaining_game_days",
                        lambda team, current_date=None: h.game_days.get(team, []))
    return h


def read(league=LEAGUE):
    return asyncio.run(svc.DailyActionsService.read(TEAM, league))


def rows(resp):
    return [(a.kind, a.player.player_id, a.counterpart.player_id if a.counterpart else None)
            for a in resp.data.actions]


def eligible(players):
    return {p.player_id: p.ir_eligible for p in svc.planner_players(state(players))}


# ---- the read ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_yahoo_team_gets_a_read_only_answer_without_any_provider_call(harness):
    resp = read(YAHOO)
    assert resp.status == ApiStatus.SUCCESS and resp.message == svc.NOT_ESPN_MESSAGE
    assert resp.data is not None and resp.data.lineup is None and resp.data.actions == []
    assert (resp.data.can_write, resp.data.write_blocked_reason) == (False, "provider_not_supported")
    assert harness.find_calls == []


@pytest.mark.unit
def test_the_pool_is_the_daily_search_for_this_team(harness):
    harness.board = state(full_board())
    read()
    assert harness.find_calls == [{"fa_count": 100, "exclude_injured": True, "b2b_only": False,
                                   "mode": StreamerMode.DAILY, "target_day": None, "avg_days": 7, "team_id": 21}]


@pytest.mark.unit
def test_a_board_read_failure_fails_the_request(harness):
    harness.board = RuntimeError("espn down")
    with pytest.raises(RuntimeError):
        read()


@pytest.mark.unit
def test_the_board_travels_with_the_rows(harness):
    harness.board = state(full_board(), can_write=False, reason="writes_disabled", version="v9")
    resp = read()
    assert resp.data.lineup.roster_version == "v9" and resp.data.roster_version == "v9"
    assert (resp.data.can_write, resp.data.write_blocked_reason) == (False, "writes_disabled")
    assert resp.data.nba_date == "2026-10-20" and resp.data.scoring_period_id == 1
    assert resp.message == "All set for today"


# ---- fill rows ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_fill_chain_is_one_row_with_the_benched_player_as_counterpart(harness):
    harness.board = state(full_board({8: player(8, "P8", UT, game=False)}))
    resp = read()
    assert rows(resp) == [("start", 11, 8)]
    row = resp.data.actions[0]
    assert row.id == "start:11" and row.title == "Start P11"
    assert [(m.player_id, m.role) for m in row.moves] == [(11, "start"), (8, "bench")]
    assert row.detail == "vs LAL · 7:30 PM · UT over P8 (no game today)"
    assert row.game_time_et == "19:30" and row.transaction is None
    assert resp.message == "1 action(s) today"


@pytest.mark.unit
def test_unfilled_bench_players_are_reported_beside_the_rows(harness):
    harness.board = state(full_board({8: player(8, "P8", UT, game=False, locked=True)}))
    resp = read()
    assert rows(resp) == []
    assert [(u.player_id, u.reason) for u in resp.data.unfilled] == [(11, "slot_holder_locked"), (12, "slot_holder_locked")]


# ---- IR rows ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_out_starter_gets_an_ir_in_row_when_the_seat_is_open(harness):
    harness.board = state([p for p in full_board({5: player(5, "P5", C, eligible=(C, UT, BE, IR), status="OUT")})
                           if p.player_id != 14])
    resp = read()
    assert rows(resp) == [("ir_in", 5, None)]
    row = resp.data.actions[0]
    assert row.id == "ir_in:5" and row.title == "Move P5 to IR" and row.detail == "OUT · frees C"
    assert [(m.from_slot_id, m.to_slot_id, m.role) for m in row.moves] == [(C, IR, "shift")]


@pytest.mark.unit
def test_a_healthy_ir_player_gets_an_ir_out_row_or_a_blocked_one(harness):
    harness.board = state([p for p in full_board({14: player(14, "P14", IR)}) if p.player_id != 12])
    resp = read()
    assert rows(resp) == [("ir_out", 14, None)]
    assert resp.data.actions[0].detail == "Healthy · to BE" and resp.data.actions[0].blocked_reason is None

    harness.board = state(full_board({14: player(14, "P14", IR)}))
    resp = read()
    row = resp.data.actions[0]
    assert (row.kind, row.blocked_reason, row.moves) == ("ir_out", "roster_full", [])
    assert row.detail == svc.ROSTER_FULL_DETAIL


# ---- the daily swap ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_no_game_player_is_swapped_for_a_free_agent_playing_tonight(harness):
    harness.board = state(full_board({12: player(12, "P12", BE, game=False, value=18.0, team="MIA")}))
    harness.pool = pool(pick(99, "Kawhi", value=20.0))
    harness.games = [LAL_HOME]
    harness.game_days = {"MIA": [0, 3]}
    resp = read()
    assert rows(resp) == [("add_drop", 99, 12)]
    row = resp.data.actions[0]
    assert row.id == "add_drop:99:12" and row.title == "Drop P12, pick up Kawhi"
    assert row.detail == "P12 has no game today (next plays Fri) · Kawhi plays tonight vs BOS · 7:30 PM · 18.0 vs 20.0"
    assert row.transaction.pickup.player_id == 99 and row.transaction.drop_player_id == 12
    assert row.player.lineup_slot_id is None and row.counterpart.lineup_slot == "BE"
    assert row.game_time_et == "19:30" and row.moves == []
    assert resp.data.streamers_error is None


@pytest.mark.unit
def test_copy_says_when_the_drop_has_no_more_games_and_survives_missing_lookups(harness):
    harness.board = state(full_board({12: player(12, "P12", BE, game=False, value=18.0, team="MIA")}))
    harness.pool = pool(pick(99, "Kawhi", value=20.0))
    harness.game_days = {"MIA": [0]}
    resp = read()
    assert resp.data.actions[0].detail == "P12 has no game today (no more games this week) · Kawhi plays tonight · 18.0 vs 20.0"


@pytest.mark.unit
def test_an_open_seat_means_a_pickup_with_nobody_to_drop(harness):
    harness.board = state([p for p in full_board() if p.player_id != 12])
    harness.pool = pool(pick(99, "Kawhi", value=5.0))
    resp = read()
    assert rows(resp) == [("add", 99, None)]
    row = resp.data.actions[0]
    assert row.id == "add:99" and row.title == "Pick up Kawhi" and row.detail == "Open roster seat · Kawhi plays tonight · 5.0 avg"
    assert row.transaction.drop_player_id is None


@pytest.mark.unit
def test_no_swap_when_everyone_on_the_roster_plays_today(harness):
    harness.board = state(full_board())
    harness.pool = pool(pick(99, "Kawhi", value=60.0))
    assert rows(read()) == []


@pytest.mark.unit
def test_a_failed_pool_fetch_keeps_the_lineup_rows_and_reports_itself(harness):
    harness.board = state(full_board({8: player(8, "P8", UT, game=False)}))
    harness.pool = RuntimeError("pool down")
    resp = read()
    assert rows(resp) == [("start", 11, 8)] and resp.data.streamers_error == "pool down"


@pytest.mark.unit
def test_no_matchup_on_the_calendar_is_an_empty_pool_not_an_error(harness):
    harness.board = state(full_board({12: player(12, "P12", BE, game=False)}))
    harness.pool = NO_POOL
    resp = read()
    assert rows(resp) == [] and resp.data.streamers_error is None


@pytest.mark.unit
@pytest.mark.parametrize("board_day", ["2026-10-21", None])
def test_the_swap_needs_the_board_and_the_pool_to_agree_on_the_day(harness, board_day):
    harness.board = state(full_board({12: player(12, "P12", BE, game=False)}), nba_date=board_day)
    harness.pool = pool(pick(99, "Kawhi"))
    resp = read()
    assert rows(resp) == [] and resp.data.streamers_error == "day_mismatch"


@pytest.mark.unit
def test_the_swap_needs_one_value_scale(harness):
    harness.board = state(full_board({12: player(12, "P12", BE, game=False)}))
    harness.pool = pool(pick(99, "Kawhi"), value_kind="cat_value")
    resp = read()
    assert rows(resp) == [] and resp.data.streamers_error == "value_kind_mismatch"


# ---- suggest_transaction (pure) ---------------------------------------------------------------------------


def suggest(players, streamers, limits=None, **kw):
    return svc.suggest_transaction(state(players, limits=limits), streamers, ir_eligible=eligible(players), **kw)


@pytest.mark.unit
def test_the_drop_is_the_lowest_value_player_without_a_game():
    players = full_board({11: player(11, "P11", BE, game=False, value=25.0), 12: player(12, "P12", BE, game=False, value=9.0),
                          8: player(8, "P8", UT, game=False, value=30.0)})
    s = suggest(players, [pick(99, value=20.0)])
    assert (s.pickup.player_id, s.drop.player_id) == (99, 12)


@pytest.mark.unit
def test_locked_ir_and_ir_eligible_players_are_never_the_drop():
    players = full_board({
        11: player(11, "P11", BE, game=False, value=1.0, locked=True),
        12: player(12, "P12", BE, game=False, value=2.0, status="OUT"),        # IR-eligible: an IR row's job
        8: player(8, "P8", UT, game=False, value=3.0, injured=True),
        14: player(14, "P14", IR, game=False, value=0.0, status="OUT"),
    })
    assert suggest(players, [pick(99, value=50.0)]) is None
    players[3] = player(8, "P8", UT, game=False, value=3.0)
    assert suggest(players, [pick(99, value=50.0)]).drop.player_id == 8


@pytest.mark.unit
def test_the_pickup_must_be_comparable_to_the_drop():
    players = full_board({12: player(12, "P12", BE, game=False, value=20.0)})
    assert suggest(players, [pick(99, value=17.0)]).pickup.player_id == 99
    assert suggest(players, [pick(99, value=16.9)]) is None
    assert svc.STREAMABLE_RATIO == 0.85


@pytest.mark.unit
def test_the_pickup_is_the_first_free_agent_with_a_value_who_is_not_already_rostered():
    players = full_board({12: player(12, "P12", BE, game=False, value=10.0)})
    streamers = [pick(1, "waivers guy", status="waivers", value=40.0), pick(2, "valueless", value=None),
                 pick(12, "already mine", value=40.0), pick(3, "the one", value=30.0), pick(4, "later", value=35.0)]
    assert suggest(players, streamers).pickup.player_id == 3
    assert suggest(players, []) is None


@pytest.mark.unit
def test_an_open_seat_skips_the_drop_and_the_ratio():
    players = [p for p in full_board({12: player(12, "P12", BE, game=False, value=40.0)}) if p.player_id != 11]
    s = suggest(players, [pick(99, value=5.0)])
    assert s.drop is None and s.pickup.player_id == 99


# ---- order and helpers ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rows_come_in_kind_order_then_by_tip_off(harness):
    board = full_board({
        5: player(5, "P5", C, eligible=(C, UT, BE, IR), status="OUT"),      # ir_in (IR is open once 14 leaves? no: 14 stays)
        1: player(1, "P1", PG, game=False),                                   # start P11 over P1
        2: player(2, "P2", SG, game=False),                                   # start P12 over P2
        11: player(11, "P11", BE, tip="22:00"),
        12: player(12, "P12", BE, tip="19:30"),
        14: player(14, "P14", IR),                                            # healthy on IR, roster full: blocked ir_out
    })
    harness.board = state(board)
    resp = read()
    assert [(a.kind, a.player.player_id) for a in resp.data.actions] == [
        ("ir_out", 14), ("start", 12), ("start", 11)]


@pytest.mark.unit
def test_chains_split_on_every_start():
    m = svc.Move
    chain_a = [m(11, BE, UT, role="start"), m(8, UT, BE, role="bench")]
    chain_b = [m(12, BE, PG, role="start"), m(1, PG, SG, role="shift"), m(2, SG, BE, role="bench")]
    assert svc.chains_of(chain_a + chain_b) == [chain_a, chain_b]
    assert svc.chains_of([]) == []


# ---- seats and position limits ---------------------------------------------------------------------------

PG_ID, C_ID = 1, 5


@pytest.mark.unit
def test_an_empty_ir_seat_is_not_a_seat_for_a_healthy_pickup():
    # Seven seats incl. IR; six players off IR and nobody on IR: ESPN's roster is full.
    players = [p for p in full_board({12: player(12, "P12", BE, game=False, value=10.0)}) if p.player_id != 14]
    assert svc.has_open_seat(state(players)) is False
    assert suggest(players, [pick(99, value=20.0)]).drop.player_id == 12
    # The same six players with one of them on IR: a seat opens.
    players = full_board({12: player(12, "P12", BE, game=False, value=10.0)})
    players = [p for p in players if p.player_id != 11]
    assert svc.has_open_seat(state(players)) is True
    assert suggest(players, [pick(99, value=20.0)]).drop is None


@pytest.mark.unit
def test_a_pickup_at_the_position_limit_is_skipped_for_the_next_one():
    players = [p for p in full_board({1: player(1, "P1", PG, pos=C_ID), 2: player(2, "P2", SG, pos=C_ID),
                                       5: player(5, "P5", C, pos=C_ID), 8: player(8, "P8", UT, pos=C_ID)})
               if p.player_id != 12]                                              # a seat is open
    streamers = [pick(99, "fifth C", pos=C_ID, value=40.0), pick(98, "a guard", pos=PG_ID, value=30.0)]
    assert suggest(players, streamers, limits={"5": 4, "1": -1}).pickup.player_id == 98
    assert suggest(players, streamers).pickup.player_id == 99                    # no limits known
    assert suggest(players, [streamers[0]], limits={"5": 4}) is None


@pytest.mark.unit
def test_a_swap_that_drops_the_same_default_position_still_fits():
    players = full_board({1: player(1, "P1", PG, pos=C_ID), 2: player(2, "P2", SG, pos=C_ID),
                          5: player(5, "P5", C, pos=C_ID), 8: player(8, "P8", UT, pos=C_ID, game=False, value=10.0)})
    s = suggest(players, [pick(99, "fifth C", pos=C_ID, value=20.0)], limits={"5": 4})
    assert (s.pickup.player_id, s.drop.player_id) == (99, 8)
    # ...but not when the drop plays a different default position.
    players = full_board({1: player(1, "P1", PG, pos=C_ID), 2: player(2, "P2", SG, pos=C_ID),
                          5: player(5, "P5", C, pos=C_ID), 8: player(8, "P8", UT, pos=C_ID),
                          12: player(12, "P12", BE, pos=PG_ID, game=False, value=10.0)})
    assert suggest(players, [pick(99, "fifth C", pos=C_ID, value=20.0)], limits={"5": 4}) is None
