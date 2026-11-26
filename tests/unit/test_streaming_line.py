"""
Unit tests for the streaming line (the Analysis desk plan, step 3).

services/streaming_line.py is pure; services/streaming_line_service.py is run with its
reads faked.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.errors import BadRequestError
from schemas.common import FantasyProvider
from schemas.lineup_editor import AcquisitionState
from schemas.streaming_line import StreamingLineData
from services import league_projection as lp
from services import league_projection_service as lps
from services import league_values_service as lvs
from services import streaming_line as sl
from services import streaming_line_service as sls
from services.live_projection import Run

pytestmark = pytest.mark.unit

TUE = date(2026, 12, 1)
UT = frozenset({11})
EVERYONE = frozenset({"ATL", "BOS", "DEN"})


def proj(name, team, value, timeline=(), slot=None):
    return lp.ProjPlayer(name=name, team=team, value=value, slots=frozenset({"UT"}), timeline=tuple(timeline),
                         slot=slot)


def days(n, teams=EVERYONE, start=TUE):
    return [(start + timedelta(i), teams) for i in range(n)]


# ------------------------------- what a player adds ------------------------------- #


def test_a_player_s_worth_is_what_the_lineup_loses_without_him():
    # two UT seats, three players: the weakest only plays when someone is idle
    star, mid, depth = (sl.LinePlayer(1, proj("Star", "ATL", 30), UT), sl.LinePlayer(2, proj("Mid", "BOS", 20), UT),
                        sl.LinePlayer(3, proj("Depth", "DEN", 10), UT))
    two = [(TUE, EVERYONE), (TUE + timedelta(1), frozenset({"ATL", "DEN"}))]
    worth = sl.worth_to_lineup([star, mid, depth], two, {11: 2})
    # day 1: Star 30 - 10 (Depth sits in), Mid 20 - 10, Depth 0; day 2: Star 30, Depth 10
    assert worth == {1: 50.0, 2: 10.0, 3: 10.0}
    hurt = sl.LinePlayer(1, proj("Star", "ATL", 30, timeline=[Run(TUE, TUE + timedelta(6), 3, 0.5)]), UT)
    assert sl.worth_to_lineup([hurt], two, {11: 2})[1] == 30.0          # at his chance to play
    assert sl.worth_to_lineup([sl.LinePlayer(1, proj("IR", "ATL", 30), UT, on_ir=True)], two, {11: 2}) == {1: 0.0}


# ------------------------------- the line ------------------------------- #


def _pair():
    return sl.LinePlayer(1, proj("P1", "ATL", 30), UT), sl.LinePlayer(2, proj("P2", "BOS", 10), UT)


def test_the_line_is_what_the_last_lane_worth_running_adds():
    p1, p2 = _pair()
    streamer = sl.LineFreeAgent(9, proj("F", "DEN", 20), UT)
    out = sl.matchup_line([p1, p2], [streamer], days(2), {11: 2}, 1)
    # one add, so one lane: P2 (the weakest) holds it and F streams in for both days
    assert (out.base, out.worth_running, out.line) == (80.0, 1, 40.0)
    assert [(ln.lane, ln.holder, ln.total, ln.gain, ln.adds) for ln in out.lanes] == [(1, 2, 100.0, 20.0, 1)]
    assert out.worth == {1: 60.0, 2: 20.0}
    # the band: one add fewer and F taken, the lane keeps P2
    assert out.line_low == 20.0


def test_a_waiver_claim_holds_a_spot_once_his_waivers_clear():
    p1, p2 = _pair()
    claim = sl.LineFreeAgent(9, proj("F", "DEN", 20), UT, available_on=TUE + timedelta(1))
    out = sl.matchup_line([p1, p2], [claim], days(2), {11: 2}, 1)
    assert (out.lanes[0].total, out.line) == (90.0, 30.0)          # P2 day 1, F day 2


def test_every_lane_count_up_to_the_adds_is_tried_and_the_fewest_at_the_best_total_kept():
    roster = [sl.LinePlayer(i, proj(f"P{i}", "ATL", v), UT) for i, v in ((1, 30), (2, 12), (3, 11))]
    pool = [sl.LineFreeAgent(9, proj("F9", "DEN", 25), UT), sl.LineFreeAgent(8, proj("F8", "BOS", 14), UT)]
    out = sl.matchup_line(roster, pool, days(1), {11: 3}, 3)
    # three lanes tried; the third replaces nobody worth replacing, so two are worth running
    assert [ln.holder for ln in out.lanes] == [3, 2, 1]
    assert out.lanes[1].total == 69.0 and out.lanes[2].gain == 0.0
    assert out.worth_running == 2 and out.line == 14.0           # the second lane: F8's 14
    assert sorted(p for p, w in out.worth.items() if w < out.line) == [2, 3]


def test_a_protected_player_never_holds_a_lane_and_no_adds_means_no_line():
    p1, p2 = _pair()
    locked = sl.LinePlayer(2, p2.proj, UT, protected=True)
    out = sl.matchup_line([p1, locked], [sl.LineFreeAgent(9, proj("F", "DEN", 20), UT)], days(2), {11: 2}, 1)
    assert [ln.holder for ln in out.lanes] == [1]
    # P1 beats F, so no lane is worth running and the line sits at P1
    assert (out.worth_running, out.line, out.lanes[0].adds) == (0, 60.0, 0)
    none = sl.matchup_line([p1, p2], [sl.LineFreeAgent(9, proj("F", "DEN", 20), UT)], days(2), {11: 2}, 0)
    assert (none.lanes, none.line, none.line_low, none.base) == ((), None, None, 80.0)
    assert sl.matchup_line([p1, p2], [], [], {11: 2}, 3).line is None


# ------------------------------- the service's pieces ------------------------------- #


def test_the_slots_are_the_league_s_active_ones_by_id():
    payload = {"settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "5": 1, "11": 3, "12": 3, "13": 1,
                                                                    "4": 0}}}}
    assert sls.slot_counts(payload) == {0: 1, 5: 1, 11: 3}


def test_each_matchup_gets_its_own_adds():
    per_day = AcquisitionState(per="game_day", per_game_day=1, matchup_used=2, season_limit=None)
    # the current matchup: 6 game days less 2 used and 1 scheduled; a later one its whole 7
    assert sls.matchup_adds(per_day, 6, 6, 6, 1, None) == 3
    assert sls.matchup_adds(per_day, 7, 6, 7, 0, None) == 7
    flat = AcquisitionState(per="matchup", limit=4, matchup_used=1)
    assert sls.matchup_adds(flat, 6, 6, None, 0, None) == 3
    assert sls.matchup_adds(flat, 7, 6, None, 0, 2) == 2          # the season cap binds
    assert sls.matchup_adds(AcquisitionState(per="matchup"), 6, 6, None, 0, None) is None
    assert sls.matchup_adds(None, 6, 6, 7, 0, None) is None
    assert sls.matchup_adds(flat, 6, 6, None, 9, None) == 0


def test_the_pool_leaves_out_the_out_and_dates_a_waiver_claim():
    fa = lambda pid, injury=None, status="free_agent", until=None: SimpleNamespace(
        player_id=pid, injury_status=injury, acquisition_status=status, waivers_until=until,
        valid_positions=["PG", "G", "UT1"])
    p = proj("X", "ATL", 10)
    out = sls.free_agents_for([(fa(1), p), (fa(2, "OUT"), p), (fa(3, status="waivers", until=TUE), p),
                               (fa(4, status="waivers"), p)])
    assert [(f.espn_id, f.available_on) for f in out] == [(1, None), (3, TUE + timedelta(1))]
    assert 11 in out[0].slots and 0 in out[0].slots


def _league_info(provider=FantasyProvider.ESPN):
    return SimpleNamespace(provider=provider, league_id=7, year=2027)


GAME_DAY_ADDS = AcquisitionState(per="game_day", per_game_day=1)


def _fake(monkeypatch, listed, *, acq=GAME_DAY_ADDS):
    from services import acquisitions, roster_protection, stream_lane_service
    from services.espn_service import EspnService
    from services.lineup_read_service import ParsedEntry
    from services.stream_lane_service import LaneDay, PendingPickup

    weeks = [lp.DatedWeek(number=n, start=TUE - timedelta(1) + timedelta(7 * (n - 6)), days=(EVERYONE,) * 7)
             for n in range(6, 11)]
    payload = {"settings": {"name": "Dreamers", "scheduleSettings": {"matchupPeriodCount": 9, "playoffTeamCount": 2},
                            "rosterSettings": {"lineupSlotCounts": {"11": 2, "12": 2, "13": 1}}},
               "status": {"currentMatchupPeriod": 6}, "teams": [{"id": 1}, {"id": 2}]}
    entries = [(1, "Atlas", (ParsedEntry(101, "Star", "ATL", 11, (11, 12), False, "ACTIVE", False),
                             ParsedEntry(102, "Depth", "BOS", 12, (11, 12), False, "DAY_TO_DAY", False),
                             ParsedEntry(103, "Hurt", "DEN", 13, (11, 12, 13), True, "OUT", False))),
               (2, "Bricks", (ParsedEntry(201, "Other", "ATL", 11, (11, 12), False, "ACTIVE", False),))]

    async def read(league_info):
        return payload

    async def free(league_info, count=lvs.FREE_AGENTS_READ):
        return listed

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def game_days(year, start, end):
        return (end - start).days + 1

    async def soft(team_id):
        return frozenset({101})

    def lane_days(start, end, base_period, base_date):
        return [LaneDay(p, base_date + timedelta(p - base_period), EVERYONE,
                        datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc) + timedelta(p)) for p in range(start, end + 1)]

    roster = (proj("Star", "ATL", 30, slot="UT"), proj("Depth", "BOS", 8, slot="BE"), proj("Hurt", "DEN", 25, slot="IR"))
    monkeypatch.setattr(lps, "read_points_league", read)
    monkeypatch.setattr(lvs, "read_free_agents", free)
    monkeypatch.setattr(EspnService, "resolve_team", staticmethod(lambda info, teams: teams[0]))
    monkeypatch.setattr(lps, "parse_inputs", lambda payload, mine: ([], [], entries))
    monkeypatch.setattr(lps, "value_players", lambda rosters, info, today: (
        (lp.Roster(1, "Atlas", roster),), SimpleNamespace(as_of=TUE)))
    monkeypatch.setattr(lvs, "value_free_agents", lambda fas, info, snap, rostered: [
        (f, proj(f.name, f.team, f.avg_points)) for f in fas])
    monkeypatch.setattr(lps, "dated_calendar", lambda: tuple(weeks))
    monkeypatch.setattr(sls, "run_db", direct)
    monkeypatch.setattr(sls, "run_cpu", direct)
    monkeypatch.setattr(acquisitions, "matchup_game_days", game_days)
    monkeypatch.setattr(acquisitions, "parse_acquisitions", lambda payload, team: acq)
    monkeypatch.setattr(roster_protection, "ids_soft", soft)
    monkeypatch.setattr(stream_lane_service, "_days", lane_days)
    monkeypatch.setattr(stream_lane_service, "_pending_pickups", lambda team_id: [
        PendingPickup(1, 3, TUE + timedelta(2), 900, 102)])


def _fa(pid, name, team, avg):
    return SimpleNamespace(player_id=pid, name=name, team=team, avg_points=avg, injury_status=None,
                           acquisition_status="free_agent", waivers_until=None, valid_positions=["PG", "UT1"])


def test_the_service_works_out_the_line_matchup_by_matchup(monkeypatch):
    _fake(monkeypatch, [_fa(901, "Streamer", "DEN", 20.0)])
    # Tuesday, after the day's first tip: the current matchup plans Wednesday to Sunday
    now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    out = asyncio.run(sls.streaming_line(5, _league_info(), window="next_4", today=TUE, now=now))
    StreamingLineData.model_validate(out)
    assert (out["key"], out["first_matchup"], out["last_matchup"]) == ("next_4", 6, 9)
    m6, m7 = out["matchups"][:2]
    assert (m6["start"], m6["end"], m6["days"]) == ("2026-12-02", "2026-12-06", 5)
    # 7 game days, one add scheduled; Star is protected, Hurt is on IR: Depth alone can hold a lane
    assert (m6["adds"], m7["adds"]) == (6, 7)
    assert [ln["holder"] for ln in m6["lanes"]] == ["Depth"]
    assert m6["worth_running"] == 1 and m6["line"] == 100.0       # the streamer's 20 a day
    assert not any(m["playoffs"] for m in out["matchups"])
    rows = {r["name"]: r for r in out["players"]}
    assert rows["Star"]["protected"] and rows["Hurt"]["on_ir"] and rows["Hurt"]["under_line"] is None
    assert rows["Depth"]["under_line"] is True and rows["Star"]["under_line"] is False
    assert rows["Depth"]["injury_status"] == "DAY_TO_DAY" and rows["Star"]["injury_status"] is None
    assert len(rows["Star"]["by_matchup"]) == 4 and out["line"] == sum(m["line"] for m in out["matchups"])
    assert out["players"][0]["name"] == "Star"
    assert out["free_agents_read"] == 1 and out["notes"][-1].startswith("The line assumes")
    final = asyncio.run(sls.streaming_line(5, _league_info(), window="playoffs", today=TUE, now=now))
    assert [(m["matchup"], m["playoffs"], m["adds"], m["days"]) for m in final["matchups"]] == [(10, True, 7, 7)]


def test_the_service_refuses_a_long_window_and_a_league_it_can_t_read(monkeypatch):
    _fake(monkeypatch, [_fa(901, "Streamer", "DEN", 20.0)])
    with pytest.raises(BadRequestError) as err:
        asyncio.run(sls.streaming_line(5, _league_info(), first_matchup=6, last_matchup=10, today=TUE))
    assert err.value.error_code == "LEAGUE_STREAMING_LINE_TOO_LONG"
    with pytest.raises(BadRequestError) as err:
        asyncio.run(sls.streaming_line(5, _league_info(), window="rest_of_season", today=TUE))
    assert err.value.error_code == "LEAGUE_STREAMING_LINE_TOO_LONG"
    with pytest.raises(BadRequestError) as err:
        asyncio.run(sls.streaming_line(5, _league_info(FantasyProvider.YAHOO), today=TUE))
    assert err.value.error_code == "LEAGUE_PROJECTION_ESPN_ONLY"


def test_without_free_agents_there_is_no_line_and_the_notes_say_so(monkeypatch):
    _fake(monkeypatch, None, acq=None)
    now = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
    out = asyncio.run(sls.streaming_line(5, _league_info(), today=TUE, now=now))
    assert out["line"] is None and all(r["under_line"] is None for r in out["players"])
    assert out["matchups"][0]["start"] == "2026-12-01"               # before today's first tip: today counts
    assert any(n.startswith("ESPN's free agent list didn't load") for n in out["notes"])
    assert any(n.startswith("Court Vision couldn't read this league's add limits") for n in out["notes"])
