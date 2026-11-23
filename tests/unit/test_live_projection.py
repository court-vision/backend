"""
Reading the live projection: a stored timeline priced on the calendar, and a
pool valued on a morning's snapshot.

The data platform writes the rows (its `live_projection` pipeline); these are
the same shapes, built by hand.
"""

from datetime import date, timedelta

import pytest

from services import live_projection as live
from services.draft_congestion import SampleWeek
from services.draft_in_season import DatedWeek
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS

pytestmark = pytest.mark.unit

MON = date(2026, 11, 16)


def _line(pts=20.0, **stats) -> StatLine:
    base = dict(pts=pts, reb=5.0, ast=4.0, stl=1.0, blk=0.5, tov=2.0, fgm=7.0, fga=15.0,
                fg3m=2.0, fg3a=5.0, ftm=4.0, fta=5.0, min=32.0)
    base.update(stats)
    return StatLine(**base, gp=1.0)


def _calendar(weeks=4) -> tuple[DatedWeek, ...]:
    """Weeks 5 on, from Monday Nov 16: DEN plays Mon/Wed/Fri, BOS Tue/Thu/Sat/Sun."""
    den, bos = frozenset({"DEN"}), frozenset({"BOS"})
    days = (den, bos, den, bos, den, bos, bos)
    return tuple(
        DatedWeek(start=MON + timedelta(days=7 * i), week=SampleWeek(number=5 + i, days=days))
        for i in range(weeks)
    )


def _player(pid, team, timeline, *, pts=20.0, games=None, name=None) -> live.LivePlayer:
    runs = live.parse_timeline(timeline)
    return live.LivePlayer(
        player_id=pid, name=name or f"P{pid}", espn_id=9000 + pid, name_normalized=f"p{pid}",
        team=team, line=_line(pts), games=games if games is not None else 0.0, timeline=runs,
    )


# ---- the timeline ---------------------------------------------------------------------


def test_a_timeline_is_read_as_dated_runs_and_a_bad_entry_is_left_out():
    runs = live.parse_timeline([
        ["2026-11-20", "2026-12-04", 7, 0.9, 33.1, 1.0],
        ["2026-11-16", "2026-11-18", 2, 0.0, 30.0, 1.0],
        ["not a day", "2026-12-10", 3, 0.5, 30.0, 1.0],
        ["2026-12-06", "2026-12-05", 1, 0.5, 30.0, 1.0],       # ends before it starts
        ["2026-12-06"],
    ])
    assert [(r.first, r.last, r.games, r.chance) for r in runs] == [
        (date(2026, 11, 16), date(2026, 11, 18), 2, 0.0),
        (date(2026, 11, 20), date(2026, 12, 4), 7, 0.9),
    ]
    assert live.parse_timeline(None) == () and live.parse_timeline({"not": "a list"}) == ()
    # A chance is a chance
    assert live.parse_timeline([["2026-11-16", "2026-11-16", 1, 1.4, 30, 1]])[0].chance == 1.0


def test_a_game_takes_the_chance_of_the_run_it_falls_in():
    runs = live.parse_timeline([
        ["2026-11-16", "2026-11-18", 2, 0.0, 30.0, 1.0],
        ["2026-11-20", "2026-12-04", 7, 0.9, 33.0, 1.0],
    ])
    assert live.chance_on(runs, date(2026, 11, 18)) == 0.0
    assert live.chance_on(runs, date(2026, 11, 27)) == 0.9
    # A game moved onto a day between two runs takes the earlier one; past the last, the last
    assert live.chance_on(runs, date(2026, 11, 19)) == 0.0
    assert live.chance_on(runs, date(2026, 12, 20)) == 0.9
    # Before any of them, the first
    assert live.chance_on(runs, date(2026, 11, 10)) == 0.0
    assert live.chance_on((), date(2026, 11, 16)) == 0.0


def test_his_games_fall_in_the_weeks_he_is_back_for():
    days = live.team_days(_calendar(), MON)["DEN"]
    assert days[:3] == [(MON, 5), (MON + timedelta(days=2), 5), (MON + timedelta(days=4), 5)]
    # Out through the first two weeks, then back at nine games in ten
    runs = live.parse_timeline([
        ["2026-11-16", "2026-11-27", 6, 0.0, 34.0, 1.0],
        ["2026-11-30", "2026-12-11", 6, 0.9, 34.0, 1.0],
    ])
    by_week = live.games_by_week(runs, days)
    assert by_week == {5: 0.0, 6: 0.0, 7: pytest.approx(2.7), 8: pytest.approx(2.7)}
    assert live.games_by_week((), days) == {}


def test_the_calendar_from_a_later_day_drops_the_games_already_played():
    thursday = MON + timedelta(days=3)
    days = live.team_days(_calendar(weeks=1), thursday)
    assert [d for d, _w in days["DEN"]] == [MON + timedelta(days=4)]
    assert len(days["BOS"]) == 3


def test_without_his_team_on_the_calendar_the_runs_count_themselves():
    runs = live.parse_timeline([
        ["2026-11-10", "2026-11-19", 5, 0.8, 30.0, 1.0],       # four of its ten days are from Monday on
        ["2026-11-21", "2026-11-30", 4, 0.5, 30.0, 1.0],
    ])
    assert live.games_from(runs, MON) == pytest.approx(0.8 * 5 * 4 / 10 + 0.5 * 4)
    assert live.games_from(runs, date(2026, 12, 1)) == 0.0


# ---- a pool valued on a snapshot -----------------------------------------------------------


def _pool_row(pid, pts, gp=70, team="DEN") -> PoolRow:
    line = _line(pts)
    fpts = round(DEFAULT_POINTS.score(line), 1)
    return PoolRow(id=pid, name=f"Pool {pid}", team=team, gp=gp, line=line, fpts_avg=fpts,
                   fpts_total=round(fpts * gp, 1), espn_id=9000 + pid, season="2025-26")


def test_a_snapshot_values_every_player_it_carries_and_leaves_the_rest():
    healthy = [["2026-11-16", "2026-12-13", 12, 0.95, 34.0, 1.0]]
    out_two_weeks = [["2026-11-16", "2026-11-27", 6, 0.0, 34.0, 1.0],
                     ["2026-11-30", "2026-12-11", 6, 0.9, 34.0, 1.0]]
    snapshot = live.LiveSnapshot(as_of=MON, version="live-test", players={
        1: _player(1, "DEN", healthy, pts=27.0),
        2: _player(2, "DEN", out_two_weeks, pts=31.0),
        3: _player(3, "BOS", [["2026-11-17", "2026-12-13", 16, 0.5, 20.0, 1.0]], pts=11.0, name="New Signing"),
        4: _player(4, "SEA", [["2026-11-16", "2026-11-25", 5, 0.8, 30.0, 1.0]], pts=15.0),
    })
    pool = {1: _pool_row(1, 24.0), 2: _pool_row(2, 30.0), 9: _pool_row(9, 18.0)}
    source = {1: "projection", 2: "baseline", 9: "projection"}

    applied = live.apply(pool, source, snapshot, MON, _calendar())

    assert (applied.as_of, applied.version) == (MON, "live-test")
    # His line is the snapshot's, his name and ids the pool's
    assert pool[1].line.pts == 27.0 and pool[1].name == "Pool 1" and pool[1].season is None
    assert pool[1].fpts_avg == round(DEFAULT_POINTS.score(_line(27.0)), 1)
    assert source == {1: "live", 2: "live", 3: "live", 4: "live", 9: "projection"}
    # Twelve games at 0.95; the injured player's six at 0.9 are all in the last two weeks
    assert applied.games_left[1] == pytest.approx(11.4)
    assert applied.week_games[2] == {5: 0.0, 6: 0.0, 7: pytest.approx(2.7), 8: pytest.approx(2.7)}
    assert applied.games_left[2] == pytest.approx(5.4)
    # A player the pool did not have comes with the snapshot's name, on its team
    assert pool[3].name == "New Signing" and pool[3].team == "BOS" and pool[3].espn_id == 9003
    assert applied.games_left[3] == pytest.approx(8.0) and pool[3].gp == 8
    # A team the calendar does not carry: the timeline's own count, and no weeks to place them in
    assert applied.games_left[4] == pytest.approx(4.0) and applied.week_games[4] == {}
    # The one the snapshot does not carry is as it was
    assert pool[9].line.pts == 18.0 and 9 not in applied.games_left


def test_a_snapshot_from_yesterday_is_priced_from_today():
    # Built Sunday night's morning: its first run covers a game already played
    timeline = [["2026-11-15", "2026-12-13", 13, 0.9, 34.0, 1.0]]
    snapshot = live.LiveSnapshot(as_of=MON - timedelta(days=1), version=None,
                                 players={1: _player(1, "DEN", timeline)})
    pool, source = {1: _pool_row(1, 24.0)}, {1: "projection"}
    applied = live.apply(pool, source, snapshot, MON, _calendar())
    assert applied.games_left[1] == pytest.approx(12 * 0.9)
