"""
`MatchupHistoryService.get_daily_matchup`: a past day asks for both sides'
dated rosters and hands them to the day builder; today, future days and
non-ESPN teams do not. The builder itself is tested in test_matchup_days.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from schemas.common import ApiStatus, FantasyProvider
from schemas.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
from schemas.matchup import DailyMatchupResp, MatchupData, MatchupResp, MatchupTeamResp
from services import matchup_days
from services import matchup_history_service as module
from services.matchup_service import MatchupService

TODAY = date(2026, 11, 10)


def _md():
    return MatchupData(
        matchup_period=4, matchup_period_start="2026-11-09", matchup_period_end="2026-11-15",
        your_team=MatchupTeamResp(team_name="You", team_id=1, current_score=0, projected_score=0, roster=[]),
        opponent_team=MatchupTeamResp(team_name="Opp", team_id=5, current_score=0, projected_score=0, roster=[]),
        projected_winner="You", projected_margin=0,
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
    w = SimpleNamespace(built=[], provider=FantasyProvider.ESPN, found={}, rosters_calls=[])

    async def fake_matchup(user_id, team_id, avg_window="season"):
        return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=_md())

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_build(md, team_id, target_date, period_start, rosters=None):
        w.built.append((target_date, rosters))
        return DailyMatchupResp(status=ApiStatus.SUCCESS, message="built", data=None)

    async def credentials_for(team_id):
        return SimpleNamespace(provider=w.provider, league_id=1, year=2027, team_name="You")

    async def rosters_for_matchup(league_info, sides, target):
        w.rosters_calls.append((dict(sides), target))
        return w.found

    monkeypatch.setattr(MatchupService, "get_matchup_by_team_id", staticmethod(fake_matchup))
    monkeypatch.setattr(module, "run_db", direct_run_db)
    monkeypatch.setattr(module, "_nba_today", lambda: TODAY)
    monkeypatch.setattr(module, "_fantasy_today", lambda: TODAY)
    monkeypatch.setattr(MatchupService, "_build_daily_from_db", staticmethod(fake_build))
    monkeypatch.setattr(module.TeamService, "credentials_for", staticmethod(credentials_for))
    from services import lineup_snapshot_service
    monkeypatch.setattr(lineup_snapshot_service.LineupSnapshotService, "rosters_for_matchup", staticmethod(rosters_for_matchup))
    return w


def daily(target):
    return asyncio.run(MatchupService.get_daily_matchup(42, 7, target))


@pytest.mark.unit
def test_a_past_day_hands_both_dated_sides_to_the_builder(world):
    world.found = {1: _snapshot(1), 5: _snapshot(5, source="provider_history")}
    daily(date(2026, 11, 9))
    assert world.rosters_calls == [({1: "You", 5: "Opp"}, date(2026, 11, 9))]
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
    monkeypatch.setattr(module, "_fantasy_today", matchup_days.fantasy_today)    # both real clock rules
    monkeypatch.setattr(module, "_nba_today", matchup_days.nba_today)
    world.found = {1: _snapshot(1), 5: _snapshot(5)}
    with freeze_time("2026-11-10T08:00:00Z"):           # 3 AM ET: the game date is still 11-09
        daily(date(2026, 11, 9))
    assert world.rosters_calls == [({1: "You", 5: "Opp"}, date(2026, 11, 9))]
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
    async def boom(league_info, sides, target):
        raise RuntimeError("db down")

    from services import lineup_snapshot_service
    monkeypatch.setattr(lineup_snapshot_service.LineupSnapshotService, "rosters_for_matchup", staticmethod(boom))
    daily(date(2026, 11, 9))
    assert world.built[0][1] is None
