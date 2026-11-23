"""
`MatchupHistoryService.get_daily_matchup` and `get_weekly_matchup`: a past day
asks for both sides' dated rosters and hands them to the day builder; today,
future days and non-ESPN teams do not. Today is ESPN's own day when the
matchup read says it. The builders are tested in test_matchup_days and the
integration suite.
"""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from schemas.common import ApiStatus, FantasyProvider
from schemas.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
from schemas.matchup import DailyMatchupResp, MatchupData, MatchupResp, MatchupTeamResp, WeeklyMatchupResp
from services import matchup_days
from services import matchup_history_service as module
from services.matchup_service import MatchupService

TODAY = date(2026, 11, 10)


def _md(scoring_period_id=None, source="unknown"):
    return MatchupData(
        matchup_period=4, matchup_period_start="2026-11-09", matchup_period_end="2026-11-15",
        your_team=MatchupTeamResp(team_name="You", team_id=1, current_score=0, projected_score=0, roster=[]),
        opponent_team=MatchupTeamResp(team_name="Opp", team_id=5, current_score=0, projected_score=0, roster=[]),
        projected_winner="You", projected_margin=0,
        scoring_period_id=scoring_period_id, scoring_period_source=source,
    )


def _snapshot(team_id, source="snapshot"):
    return LineupSnapshot(
        provider=FantasyProvider.ESPN, provider_league_id="1", season=2027, provider_team_id=team_id,
        team_name=f"T{team_id}", scoring_period_id=21, nba_date="2026-11-09", source=source,
        captured_at="2026-11-10T08:05:00+00:00" if source == "snapshot" else None,
        players=[LineupSnapshotPlayer(player_id=10 * team_id, name="P", team="DET", position="PG",
                                      lineup_slot_id=0, lineup_slot="PG")],
    )


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(built=[], weeks=[], provider=FantasyProvider.ESPN, found={}, rosters_calls=[], md=_md())

    async def fake_matchup(user_id, team_id, avg_window="season"):
        return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=w.md)

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_build(md, team_id, target_date, period_start, today, rosters=None):
        w.built.append((target_date, rosters))
        w.today = today
        return DailyMatchupResp(status=ApiStatus.SUCCESS, message="built", data=None)

    def fake_build_week(md, team_id, dates, period_start, today, rosters_by_day=None):
        w.weeks.append((dates, today, rosters_by_day))
        return WeeklyMatchupResp(status=ApiStatus.SUCCESS, message="built", data=None)

    async def credentials_for(team_id):
        return SimpleNamespace(provider=w.provider, league_id=1, year=2027, team_name="You")

    async def rosters_for_matchup_days(league_info, sides, targets):
        w.rosters_calls.append((dict(sides), list(targets)))
        return {d: w.found for d in targets}

    monkeypatch.setattr(MatchupService, "get_matchup_by_team_id", staticmethod(fake_matchup))
    monkeypatch.setattr(module, "run_db", direct_run_db)
    monkeypatch.setattr(module, "_fantasy_today", lambda: TODAY)
    monkeypatch.setattr(MatchupService, "_build_daily_from_db", staticmethod(fake_build))
    monkeypatch.setattr(MatchupService, "_build_weekly_from_db", staticmethod(fake_build_week))
    monkeypatch.setattr(module.TeamService, "credentials_for", staticmethod(credentials_for))
    from services import lineup_snapshot_service
    monkeypatch.setattr(lineup_snapshot_service.LineupSnapshotService, "rosters_for_matchup_days",
                        staticmethod(rosters_for_matchup_days))
    return w


def daily(target):
    return asyncio.run(MatchupService.get_daily_matchup(42, 7, target))


def weekly():
    return asyncio.run(MatchupService.get_weekly_matchup(42, 7))


@pytest.mark.unit
def test_a_past_day_hands_both_dated_sides_to_the_builder(world):
    world.found = {1: _snapshot(1), 5: _snapshot(5, source="provider_history")}
    daily(date(2026, 11, 9))
    assert world.rosters_calls == [({1: "You", 5: "Opp"}, [date(2026, 11, 9)])]
    (target, rosters), = world.built
    assert rosters.your.source == "snapshot" and rosters.your.captured_at == "2026-11-10T08:05:00+00:00"
    assert rosters.opp.source == "provider_history" and rosters.opp.captured_at is None
    assert [p.player_id for p in rosters.your.players] == [10] and [p.player_id for p in rosters.opp.players] == [50]


@pytest.mark.unit
def test_a_side_without_history_stays_current(world):
    world.found = {1: _snapshot(1)}
    daily(date(2026, 11, 9))
    (_, rosters), = world.built
    assert rosters.your.source == "snapshot"
    assert rosters.opp.source == "current" and rosters.opp.players is None


@pytest.mark.unit
def test_today_and_future_days_never_look_for_history(world):
    daily(TODAY)
    daily(date(2026, 11, 12))
    assert world.rosters_calls == [] and [r for _, r in world.built] == [None, None]


@pytest.mark.unit
def test_between_2_and_6_am_the_day_espn_just_finished_shows_its_own_rosters(world, monkeypatch):
    # The Matchup page already shows 11-09 as a past day; today's roster is ESPN's 11-10 by then.
    monkeypatch.setattr(module, "_fantasy_today", matchup_days.fantasy_today)    # the real clock rule
    world.found = {1: _snapshot(1), 5: _snapshot(5)}
    with freeze_time("2026-11-10T08:00:00Z"):           # 3 AM ET: the game date is still 11-09
        daily(date(2026, 11, 9))
    assert world.rosters_calls == [({1: "You", 5: "Opp"}, [date(2026, 11, 9)])]
    assert world.today == date(2026, 11, 10)            # and 11-09 is built as a past day
    assert world.built[0][1].your.source == "snapshot"
    with freeze_time("2026-11-10T06:30:00Z"):           # 1:30 AM ET: ESPN is still on 11-09
        daily(date(2026, 11, 9))
    assert len(world.rosters_calls) == 1 and world.built[1][1] is None


@pytest.mark.unit
def test_non_espn_teams_keep_todays_roster(world):
    world.provider = FantasyProvider.YAHOO
    daily(date(2026, 11, 9))
    assert world.rosters_calls == [] and world.built[0][1] is None


@pytest.mark.unit
def test_a_failed_lookup_falls_back_to_todays_roster(world, monkeypatch):
    async def boom(league_info, sides, targets):
        raise RuntimeError("db down")

    from services import lineup_snapshot_service
    monkeypatch.setattr(lineup_snapshot_service.LineupSnapshotService, "rosters_for_matchup_days", staticmethod(boom))
    daily(date(2026, 11, 9))
    assert world.built[0][1] is None


# ---- today is ESPN's own day ------------------------------------------------------------------


@pytest.mark.unit
def test_espns_day_decides_today_whatever_the_clock_says(world, monkeypatch):
    monkeypatch.setattr(matchup_days.schedule_service, "date_for_espn_scoring_period",
                        lambda period, season=None: date(2026, 10, 19) + timedelta(days=period))
    world.md = _md(scoring_period_id=22, source="provider")      # ESPN is on 11-10
    world.found = {1: _snapshot(1), 5: _snapshot(5)}
    monkeypatch.setattr(module, "_fantasy_today", lambda: date(2026, 11, 9))   # a clock that hasn't turned
    daily(date(2026, 11, 9))
    assert world.today == date(2026, 11, 10) and world.built[0][1].your.source == "snapshot"

    world.md = _md(scoring_period_id=21, source="provider")      # ESPN's batch is late: 11-09 is still today
    monkeypatch.setattr(module, "_fantasy_today", lambda: TODAY)
    daily(date(2026, 11, 9))
    assert world.today == date(2026, 11, 9) and world.built[1][1] is None


# ---- the week ------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_week_reads_every_past_days_lineups_at_once(world):
    world.found = {1: _snapshot(1), 5: _snapshot(5, source="provider_history")}
    weekly()
    assert world.rosters_calls == [({1: "You", 5: "Opp"}, [date(2026, 11, 9)])]   # 11-10 is today
    (dates, today, rosters), = world.weeks
    assert len(dates) == 7 and today == TODAY
    assert list(rosters) == [date(2026, 11, 9)]
    assert rosters[date(2026, 11, 9)].your.source == "snapshot"
    assert rosters[date(2026, 11, 9)].opp.source == "provider_history"


@pytest.mark.unit
def test_a_week_with_no_day_played_reads_no_lineups(world, monkeypatch):
    monkeypatch.setattr(module, "_fantasy_today", lambda: date(2026, 11, 9))   # its first day
    weekly()
    assert world.rosters_calls == []
    assert world.weeks[0][1] == date(2026, 11, 9) and world.weeks[0][2] == {}


@pytest.mark.unit
def test_a_yahoo_week_stays_on_todays_rosters(world):
    world.provider = FantasyProvider.YAHOO
    weekly()
    assert world.rosters_calls == [] and world.weeks[0][2] == {}


@pytest.mark.unit
def test_a_failed_lineup_read_leaves_the_week_on_todays_rosters(world, monkeypatch):
    async def boom(league_info, sides, targets):
        raise RuntimeError("db down")

    from services import lineup_snapshot_service
    monkeypatch.setattr(lineup_snapshot_service.LineupSnapshotService, "rosters_for_matchup_days", staticmethod(boom))
    weekly()
    assert world.weeks[0][2] == {}
