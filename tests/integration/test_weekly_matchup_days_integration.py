"""
The week's days against real SQL (`MatchupHistoryService._build_weekly_from_db`):
the morning after a game night, between ESPN's ~2 AM ET rollover and the
post-game run, last night is a past day built from the live pipeline's rows,
on the lineup that counted; today has no box score yet.
"""

from datetime import date, time

import pytest

from db.models.nba.games import Game
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from schemas.lineup_snapshots import LineupSnapshotPlayer
from schemas.matchup import MatchupData, MatchupPlayerResp, MatchupTeamResp
from services.matchup_days import DayRosters, RosterSide
from services.matchup_history_service import MatchupHistoryService

pytestmark = pytest.mark.integration

TUE, WED, THU = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
LINE = {"pts": 20, "reb": 5, "ast": 5, "stl": 1, "blk": 1, "tov": 2, "min": 30,
        "fgm": 8, "fga": 16, "fg3m": 2, "fg3a": 5, "ftm": 2, "fta": 2}


def _player(pid, name, team, slot="PG"):
    return MatchupPlayerResp(player_id=pid, name=name, team=team, position="PG", lineup_slot=slot,
                             avg_points=20.0, projected_points=0.0, games_remaining=1, injured=False)


def _dated(pid, name, team, slot_id, slot):
    return LineupSnapshotPlayer(player_id=pid, name=name, team=team, position="PG",
                                lineup_slot_id=slot_id, lineup_slot=slot)


def _md():
    # ESPN's rosters as they stand on Wednesday: Benched Guy was cut after Tuesday's games.
    return MatchupData(
        matchup_period=1, matchup_period_start="2026-10-20", matchup_period_end="2026-10-26",
        your_team=MatchupTeamResp(team_name="You", team_id=1, current_score=0, projected_score=0,
                                  roster=[_player(101, "Starter Guard", "BOS"), _player(102, "Bench Guard", "DET", "BE")]),
        opponent_team=MatchupTeamResp(team_name="Opp", team_id=5, current_score=0, projected_score=0,
                                      roster=[_player(103, "Their Center", "DET")]),
        projected_winner="You", projected_margin=0,
    )


@pytest.fixture
def last_night(integration_db):
    for abbrev in ("BOS", "DET", "NYK"):
        NBATeam.get_or_create(id=abbrev, defaults={"name": abbrev, "city": abbrev,
                                                   "conference": "East", "division": "Central"})
    for nba_id, espn_id, name in ((1, 101, "Starter Guard"), (2, 102, "Bench Guard"),
                                  (3, 103, "Their Center"), (4, 104, "Cut Wing")):
        Player.create(id=nba_id, espn_id=espn_id, name=name, name_normalized=name.lower())
    Game.create(game_id="0022600001", game_date=TUE, season="2026-27", home_team="DET", away_team="BOS",
                status="final", start_time_et=time(19, 0))
    Game.create(game_id="0022600002", game_date=WED, season="2026-27", home_team="NYK", away_team="BOS",
                status="scheduled", start_time_et=time(19, 30))
    # The post-game run has written only one of Tuesday's lines; the rest are still live rows.
    PlayerGameStats.create(player=3, team="DET", game_date=TUE, game="0022600001", season="2026-27",
                           season_type="Regular Season", fpts=40, **LINE)
    for nba_id, team, fpts in ((1, "BOS", 31), (2, "DET", 25), (3, "DET", 99), (4, "BOS", 12)):
        LivePlayerStats.create(player=nba_id, game_id="0022600001", game_date=TUE, game_status=3, fpts=fpts, **LINE)


def test_last_night_is_a_settled_past_day_on_the_lineup_that_counted(last_night):
    tuesday = DayRosters(your=RosterSide(players=[
        _dated(101, "Starter Guard", "BOS", 0, "PG"),
        _dated(104, "Cut Wing", "BOS", 11, "UT"),
        _dated(102, "Bench Guard", "DET", 12, "BE"),
    ], source="snapshot", captured_at="2026-10-21T07:10:00+00:00"))

    week = MatchupHistoryService._build_weekly_from_db(
        _md(), 7, [TUE, WED, THU], TUE, WED, {TUE: tuesday},
    ).data
    tue, wed, thu = week.days
    assert [d.day_type for d in week.days] == ["past", "today", "future"]

    mine = {p.player_id: p for p in tue.your_team.roster}
    assert {pid: p.fpts for pid, p in mine.items()} == {101: 31.0, 104: 12.0, 102: 25.0}
    assert [p.lineup_slot for p in tue.your_team.roster] == ["PG", "UT", "BE"]
    assert tue.your_team.total_fpts == 43.0                  # the bench's 25 never counted
    assert tue.your_team.roster_source == "snapshot"
    # The opponent's side had no record: today's roster, every point, and the stored line wins.
    assert tue.opponent_team.roster_source == "current"
    assert tue.opponent_team.total_fpts == 40.0

    assert [p.fpts for p in wed.your_team.roster] == [None, None]   # Wednesday's games haven't tipped
    assert {p.player_id for p in wed.your_team.roster} == {101, 102}
    assert thu.your_team.total_fpts is None
