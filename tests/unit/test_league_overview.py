"""The league as its managers read it (services/league_overview.py): standings, the
playoff picture, the schedule and rosters from one ESPN read, and the league's adds
and drops off the move ledger.

The payload below follows ESPN's league endpoint with the mTeam, mSettings, mSchedule
and mRoster views: teams[] with record.overall, playoffSeed and transactionCounter,
settings.scheduleSettings, schedule[] with home/away sides, and status."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from services import league_overview as lo

pytestmark = pytest.mark.unit

WEEK_1 = date(2026, 10, 20)


def team(tid, name, w, l, t=0, seed=0, pf=0.0, pa=0.0, streak=("WIN", 1), adds=None, season_adds=0, roster=None):
    out = {
        "id": tid, "name": name, "abbrev": name[:3].upper(), "playoffSeed": seed, "waiverRank": tid,
        "record": {"overall": {"wins": w, "losses": l, "ties": t, "pointsFor": pf, "pointsAgainst": pa,
                               "streakType": streak[0], "streakLength": streak[1], "gamesBack": 0.0}},
        "transactionCounter": {"acquisitions": season_adds, "matchupAcquisitionTotals": adds or {}},
    }
    if roster is not None:
        out["roster"] = {"entries": roster}
    return out


def side(tid, points=None, cats=None):
    out = {"teamId": tid, "totalPoints": points}
    if cats is not None:
        out["cumulativeScore"] = {"wins": cats[0], "losses": cats[1], "ties": cats[2]}
    return out


def game(period, home, away, winner="UNDECIDED", tier="NONE"):
    return {"id": period * 10 + home["teamId"], "matchupPeriodId": period, "home": home, "away": away,
            "winner": winner, "playoffTierType": tier}


def entry(espn_id, name, slot, eligible, pro=1, injury="ACTIVE"):
    return {"lineupSlotId": slot, "playerId": espn_id, "playerPoolEntry": {"player": {
        "id": espn_id, "fullName": name, "proTeamId": pro, "eligibleSlots": eligible,
        "injuryStatus": injury, "injured": injury not in ("ACTIVE", None), "defaultPositionId": 1}}}


def payload(**over):
    """A five-team points league in matchup 3 of 4 regular-season matchups; three make the playoffs."""
    teams = [
        team(1, "Atlas", 1, 1, seed=3, pf=210.5, pa=200.0, streak=("LOSS", 1), adds={"3": 2}, season_adds=5),
        team(2, "Bricks", 2, 0, seed=1, pf=230.0, pa=180.0, streak=("WIN", 2), adds={"3": 1}, season_adds=3),
        team(3, "Cookers", 1, 1, seed=2, pf=220.0, pa=215.0),
        team(4, "Dunkers", 0, 2, seed=4, pf=190.0, pa=230.0, streak=("LOSS", 2), adds={"3": 4}, season_adds=9),
        team(5, "Eurosteps", 0, 2, seed=5, pf=180.0, pa=205.0, streak=("LOSS", 2)),
    ]
    schedule = [
        game(1, side(1, 110.0), side(2, 120.0), "AWAY"),
        game(2, side(3, 95.0), side(1, 100.5), "AWAY"),
        game(3, side(1, 40.0), side(4, 35.5)),
        game(3, side(2, 50.0), side(3, 51.0)),
        game(4, side(5), side(1)),
    ]
    data = {
        "teams": teams,
        "schedule": schedule,
        "settings": {
            "name": "Atlas League",
            "scoringSettings": {"scoringType": "H2H_POINTS"},
            "scheduleSettings": {"matchupPeriodCount": 4, "playoffTeamCount": 3,
                                 "playoffSeedingRule": "H2H_RECORD",
                                 "matchupPeriods": {str(i): [i] for i in range(1, 7)}},
        },
        "status": {"currentMatchupPeriod": 3, "latestScoringPeriod": 16},
    }
    data.update(over)
    return data


def weeks(matchup: int):
    start = WEEK_1 + timedelta(days=7 * (matchup - 1))
    return start, start + timedelta(days=6)


# ---------------------------- standings and the playoff picture ---------------------------- #


def test_standings_follow_espn_s_seed_and_carry_each_record():
    rows = lo.standings(payload(), mine=1, categories=False)
    assert [r["team_name"] for r in rows] == ["Bricks", "Cookers", "Atlas", "Dunkers", "Eurosteps"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5]
    atlas = rows[2]
    assert atlas["mine"] is True and (atlas["wins"], atlas["losses"], atlas["ties"]) == (1, 1, 0)
    assert atlas["win_pct"] == 0.5 and atlas["streak"] == "L1" and atlas["games_back"] == 1.0
    assert atlas["points_for"] == 210.5 and atlas["adds_this_matchup"] == 2 and atlas["adds_season"] == 5
    assert rows[0]["games_back"] == 0.0 and rows[0]["mine"] is False


def test_teams_without_a_seed_rank_by_record_then_points():
    data = payload()
    for t in data["teams"]:
        t["playoffSeed"] = 0
    rows = lo.standings(data, mine=1, categories=False)
    assert [r["team_name"] for r in rows] == ["Bricks", "Cookers", "Atlas", "Dunkers", "Eurosteps"]


def test_a_tie_is_half_a_win_and_half_a_game():
    data = payload(teams=[team(1, "A", 1, 0, 1, seed=1), team(2, "B", 1, 1, 0, seed=2)])
    a, b = lo.standings(data, mine=None, categories=False)
    assert a["win_pct"] == 0.75 and b["games_back"] == 0.5


def test_the_playoff_picture_says_who_is_in_and_by_how_much():
    rows = lo.standings(payload(), mine=1, categories=False)
    picture = lo.playoff_picture(rows, 3, categories=False)
    assert picture == {"playoff_teams": 3, "last_in": "Atlas", "first_out": "Dunkers"}
    atlas, dunkers = rows[2], rows[3]
    assert atlas["playoff_spot"] is True and atlas["games_ahead_of_first_out"] == 1.0
    assert atlas["points_ahead_of_first_out"] == 20.5
    assert dunkers["playoff_spot"] is False and dunkers["games_behind_last_in"] == 1.0
    assert dunkers["points_behind_last_in"] == 20.5
    assert rows[0]["games_ahead_of_first_out"] == 2.0


def test_a_league_where_everyone_makes_it_says_so():
    rows = lo.standings(payload(), mine=1, categories=False)
    picture = lo.playoff_picture(rows, 8, categories=False)
    assert all(r["playoff_spot"] for r in rows) and "Every team" in picture["note"]


# ---------------------------- the schedule ---------------------------- #


def test_my_schedule_has_results_scores_and_the_opponents_ahead():
    out = lo.parse_league(payload(), 1, categories=False, period_dates=weeks)
    sched = out["my_schedule"]
    assert [g["matchup"] for g in sched] == [1, 2, 3, 4]
    first = sched[0]
    assert first["status"] == "final" and first["result"] == "L" and first["opponent"] == "Bricks"
    assert (first["score"], first["opponent_score"]) == (110.0, 120.0)
    assert first["start"] == "2026-10-20" and first["end"] == "2026-10-26"
    assert sched[1]["result"] == "W" and sched[1]["score"] == 100.5          # an away win
    now = sched[2]
    assert now["status"] == "current" and now["opponent"] == "Dunkers" and now["opponent_rank"] == 4
    assert "result" not in now and now["score"] == 40.0
    ahead = sched[3]
    assert ahead["status"] == "upcoming" and ahead["opponent"] == "Eurosteps" and "score" not in ahead


def test_the_scoreboard_is_this_matchup_s_pairings():
    out = lo.parse_league(payload(), 1, categories=False, period_dates=weeks)
    assert out["scoreboard"] == [
        {"home": "Atlas", "home_rank": 3, "home_score": 40.0, "away": "Dunkers", "away_rank": 4, "away_score": 35.5},
        {"home": "Bricks", "home_rank": 1, "home_score": 50.0, "away": "Cookers", "away_rank": 2, "away_score": 51.0},
    ]
    assert out["season"] == {"current_matchup": 3, "regular_season_matchups": 4, "in_playoffs": False,
                             "regular_season_matchups_left": 1, "current_start": "2026-11-03",
                             "current_end": "2026-11-09"}
    assert out["playoffs"]["seeded_by"] == "record" and out["league_name"] == "Atlas League"


def test_a_live_score_wins_over_the_stored_one_in_the_current_matchup():
    data = payload()
    data["schedule"][2]["home"]["totalPointsLive"] = 61.25
    data["schedule"][0]["home"]["totalPointsLive"] = 999.0             # a finished matchup keeps its final
    out = lo.parse_league(data, 1, categories=False, period_dates=weeks)
    assert out["scoreboard"][0]["home_score"] == 61.2
    assert out["my_schedule"][0]["score"] == 110.0


def test_playoff_matchups_say_which_bracket():
    data = payload(status={"currentMatchupPeriod": 5})
    data["schedule"].append(game(5, side(1, 0.0), side(2, 0.0), tier="WINNERS_BRACKET"))
    out = lo.parse_league(data, 1, categories=False, period_dates=weeks)
    assert out["season"]["in_playoffs"] is True
    assert out["my_schedule"][-1]["playoffs"] == "winners"


def test_a_missing_calendar_leaves_the_dates_out_not_the_matchups():
    def none(_):
        raise FileNotFoundError("no calendar")
    out = lo.parse_league(payload(), 1, categories=False, period_dates=none)
    assert len(out["my_schedule"]) == 4 and out["my_schedule"][0]["start"] is None


def test_a_category_league_counts_categories_and_has_no_points():
    data = payload()
    data["settings"]["scoringSettings"]["scoringType"] = "H2H_CATEGORY"
    data["schedule"] = [game(1, side(1, cats=(5, 3, 1)), side(2, cats=(3, 5, 1)), "HOME"),
                        game(3, side(1, cats=(2, 6, 1)), side(4, cats=(6, 2, 1)))]
    assert lo.league_format(data) == "categories"
    out = lo.parse_league(data, 1, categories=True, period_dates=weeks)
    assert "points_for" not in out["standings"][0] and "points_ahead_of_first_out" not in out["standings"][0]
    assert out["my_schedule"][0]["result"] == "W" and out["my_schedule"][0]["score"] == 5.0
    assert out["scoreboard"][0]["home_score"] == 2.0 and out["format"] == "categories"


# ---------------------------- rosters and the whole read ---------------------------- #


def test_a_roster_names_slots_positions_and_injuries():
    data = payload()
    data["teams"][3]["roster"] = {"entries": [
        entry(901, "Bench Guy", 12, [0, 1, 5, 11, 12]),
        entry(902, "Big Man", 4, [4, 11, 12], pro=20, injury="DAY_TO_DAY"),
    ]}
    name, rows = lo.roster_rows(data, 4)
    assert name == "Dunkers"
    assert rows == [
        {"_espn": 902, "name": "Big Man", "team": "PHI", "positions": "C", "slot": "C", "injury": "DAY_TO_DAY"},
        {"_espn": 901, "name": "Bench Guy", "team": "ATL", "positions": "PG/SG", "slot": "BE", "injury": None},
    ]
    with pytest.raises(lo.LeagueReadError):
        lo.roster_rows(data, 77)


def _info(**over) -> LeagueInfo:
    base = dict(league_id=123, team_name="Atlas", espn_s2="s2", swid="{SWID}", year=2027,
                provider=FantasyProvider.ESPN)
    base.update(over)
    return LeagueInfo(**base)


@pytest.fixture
def espn(monkeypatch):
    """Stub the one ESPN read and the DB lookups around it."""
    from services import schedule_service
    from services.espn_service import EspnService
    from services.player_value_service import PlayerValueService, ValueResult
    from services.scoring.resolver import resolve_scoring

    calls = []
    data = payload()
    data["teams"][0]["roster"] = {"entries": [entry(801, "My Guard", 0, [0, 5, 11, 12])]}
    data["teams"][3]["roster"] = {"entries": [entry(901, "Their Wing", 2, [2, 6, 11, 12], injury="OUT")]}

    async def fetch(league_info, views, **kw):
        calls.append(list(views))
        return data

    async def run_db(name, fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(EspnService, "fetch_league", fetch)
    monkeypatch.setattr(lo, "run_db", run_db)
    monkeypatch.setattr(PlayerValueService, "scoring_for", lambda info, team_id=None: resolve_scoring(None))
    monkeypatch.setattr(EspnService, "_values_for",
                        lambda scoring, ids, **kw: {801: ValueResult(31.26, "rolling"), 901: ValueResult(None, None)})
    monkeypatch.setattr("services.providers.identity.nba_ids_by_espn_id", lambda ids: {801: 1001, 901: 1002})
    monkeypatch.setattr(schedule_service, "get_espn_matchup_dates", lambda periods, mp, latest=None: weeks(mp))
    return calls


def test_read_league_finds_the_user_s_team_and_reads_without_rosters(espn):
    out = asyncio.run(lo.read_league(_info(), today=date(2026, 11, 5)))
    assert espn == [["mTeam", "mSettings", "mSchedule", "mStandings"]]
    assert out["today"] == "2026-11-05" and "rosters" not in out
    assert [r["team_name"] for r in out["standings"] if r["mine"]] == ["Atlas"]


def test_read_league_values_the_rosters_asked_for(espn):
    out = asyncio.run(lo.read_league(_info(), roster_ids=[1, 4, 4], today=date(2026, 11, 5)))
    assert espn == [["mTeam", "mSettings", "mSchedule", "mStandings", "mRoster"]]
    mine, theirs = out["rosters"]
    assert mine["mine"] is True and mine["as_of"] == "now"
    assert mine["players"] == [{"player_id": 1001, "name": "My Guard", "team": "ATL", "positions": "PG",
                                "slot": "PG", "injury": None, "avg_value": 31.3}]
    assert theirs["team_name"] == "Dunkers" and theirs["players"][0]["injury"] == "OUT"
    assert theirs["players"][0]["avg_value"] is None and out["value_kind"] == "fpts"


def test_read_league_refuses_a_team_not_in_the_league_and_a_day_not_finished(espn):
    with pytest.raises(lo.LeagueReadError, match="No team 9"):
        asyncio.run(lo.read_league(_info(), roster_ids=[9], today=date(2026, 11, 5)))
    with pytest.raises(lo.LeagueReadError, match="not a finished day"):
        asyncio.run(lo.read_league(_info(), roster_ids=[1], day=date(2026, 11, 5), today=date(2026, 11, 5)))


def test_read_league_is_espn_only():
    with pytest.raises(lo.LeagueReadError, match="ESPN leagues only"):
        asyncio.run(lo.read_league(_info(provider=FantasyProvider.YAHOO), today=date(2026, 11, 5)))


def test_a_past_day_s_rosters_come_from_the_stored_snapshots(espn, monkeypatch):
    from schemas.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
    from services.lineup_snapshot_service import LineupSnapshotService

    day = date(2026, 11, 2)
    asked = {}

    async def rosters(league_info, sides, targets):
        asked.update(sides=sides, targets=targets)
        snap = LineupSnapshot(
            provider=FantasyProvider.ESPN, provider_league_id="123", season=2027, provider_team_id=4,
            team_name="Dunkers", scoring_period_id=14, nba_date=day.isoformat(), source="snapshot",
            players=[LineupSnapshotPlayer(player_id=901, nba_player_id=1002, name="Their Wing", team="ATL",
                                          position="SF", lineup_slot_id=2, lineup_slot="SF", applied_total=28.0)])
        return {day: {4: snap}}

    monkeypatch.setattr(LineupSnapshotService, "rosters_for_matchup_days", rosters)
    out = asyncio.run(lo.read_league(_info(), roster_ids=[4, 2], day=day, today=date(2026, 11, 5)))
    assert espn == [["mTeam", "mSettings", "mSchedule", "mStandings"]]                # no live rosters for a past day
    assert asked == {"sides": {4: "Dunkers", 2: "Bricks"}, "targets": [day]}
    assert out["rosters"] == [{"league_team_id": 4, "team_name": "Dunkers", "mine": False, "as_of": "2026-11-02",
                               "players": [{"player_id": 1002, "name": "Their Wing", "team": "ATL",
                                            "position": "SF", "slot": "SF", "injury": None, "points": 28.0}]}]
    assert out["rosters_missing"] == ["Bricks"]


# ---------------------------- league activity ---------------------------- #


Q = lo.ActivityQuery(team_id=21, provider="espn", provider_league_id="123", season=2027, provider_team_id=1,
                     since=date(2026, 10, 29))
THROUGH = {1: date(2026, 11, 4), 4: date(2026, 11, 4)}


def move(**fields) -> SimpleNamespace:
    base = dict(id=1, nba_date=date(2026, 11, 1), team_name="Dunkers", team_id_id=None, provider_team_id=4,
                source="outside", add_nba_id=1002, add_name="Their Wing", drop_nba_id=1003, drop_name="Old Wing",
                windows={"held_through": "2026-11-04"})
    base.update(fields)
    return SimpleNamespace(**base)


def test_a_move_says_who_made_it_and_whether_the_add_is_still_there():
    positions = {1002: "F", 1003: "G"}
    view = lo.activity_view(move(), Q, THROUGH, positions)
    assert view == {"date": "2026-11-01", "team_name": "Dunkers", "league_team_id": 4, "mine": False,
                    "add": {"player_id": 1002, "name": "Their Wing", "position": "F"},
                    "drop": {"player_id": 1003, "name": "Old Wing", "position": "G"}, "still_held": True}
    gone = lo.activity_view(move(windows={"held_through": "2026-11-02"}), Q, THROUGH, positions)
    assert gone["still_held"] is False and gone["held_through"] == "2026-11-02"
    drop_only = lo.activity_view(move(add_nba_id=None, add_name=None), Q, THROUGH, positions)
    assert drop_only["add"] is None and "still_held" not in drop_only


def test_only_the_user_s_own_moves_carry_their_source():
    mine = lo.activity_view(move(team_id_id=21, provider_team_id=1, source="lane"), Q, THROUGH, {})
    assert mine["mine"] is True and mine["source"] == "lane"
    rival_on_cv = lo.activity_view(move(team_id_id=99, source="dime"), Q, THROUGH, {})
    assert rival_on_cv["mine"] is False and "source" not in rival_on_cv


def test_the_summary_counts_each_team_s_moves_busiest_first():
    rows = [
        move(id=1, nba_date=date(2026, 11, 3)),
        move(id=2, nba_date=date(2026, 11, 1), add_nba_id=1004, add_name="Center", drop_nba_id=None, drop_name=None,
             windows={"held_through": "2026-11-02"}),
        move(id=3, team_name="Atlas", team_id_id=21, provider_team_id=1, add_nba_id=1005, add_name="Guard",
             drop_nba_id=1006, drop_name="Other"),
    ]
    out = lo.summarize_activity(rows, Q, THROUGH, {1002: "F", 1004: "C", 1005: "G"})
    assert out == [
        {"league_team_id": 4, "team_name": "Dunkers", "mine": False, "adds": 2, "drops": 1, "adds_still_held": 1,
         "last_move": "2026-11-03", "positions_added": "C 1, F 1"},
        {"league_team_id": 1, "team_name": "Atlas", "mine": True, "adds": 1, "drops": 1, "adds_still_held": 1,
         "last_move": "2026-11-01", "positions_added": "G 1"},
    ]
