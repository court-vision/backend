"""Value over a window (services/league_values.py and its service): the window's matchups and
days, each player's games and points in it, what he adds to his roster's lineup, the ranks and
tiers, and the service's reads around them."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core.errors import BadRequestError, ProviderError
from schemas.common import FantasyProvider
from schemas.league_values import LeagueValuesData
from services import league_projection as lp
from services import league_projection_service as lps
from services import league_values as lv
from services import league_values_service as lvs
from services.live_projection import Run

pytestmark = pytest.mark.unit

MON = date(2026, 11, 23)
ALL = frozenset({"ATL", "BOS", "CHI", "DEN"})


def week(number, start, days):
    return lp.DatedWeek(number=number, start=start, days=tuple(frozenset(d) for d in days))


def full_week(number, start, teams=ALL):
    return week(number, start, [teams] * 7)


def player(name, team, value, slots=("UT",), chance=1.0, timeline=(), projected=True, slot=None, unplayed=False):
    return lp.ProjPlayer(name=name, team=team, value=value, slots=frozenset(slots), chance=chance,
                         timeline=tuple(timeline), projected=projected, slot=slot, unplayed=unplayed)


# ------------------------------- a day's seats ------------------------------- #


def test_a_day_s_seats_say_what_each_player_adds_and_sum_to_the_day():
    big1 = player("Big One", "ATL", 40, slots=("C", "UT"))
    big2 = player("Big Two", "BOS", 30, slots=("C", "UT"))
    big3 = player("Big Three", "CHI", 20, slots=("C", "UT"))
    idle = player("Idle", "MIA", 50)
    roster = [big1, big2, big3, idle]
    assert lp.day_seats(roster, ALL, MON, ["C", "UT"]) == [40, 30, 0, 0]
    assert sum(lp.day_seats(roster, ALL, MON, ["C", "UT"])) == lp.day_value(roster, ALL, MON, ["C", "UT"])
    # a player locked in his slot at the tip adds the rest of his game; the one he keeps out adds nothing
    started = player("Started", "ATL", 40, slots=("C",), slot="C")
    late = player("Late", "CHI", 50, slots=("C",), slot="BE")
    assert lp.day_seats([started, late], ALL, MON, ["C"], {"ATL": 0.5}) == [20, 0]
    # with no lineup known everyone counts
    assert lp.day_seats(roster, ALL, MON, []) == [40, 30, 20, 0]


# ------------------------------- the window ------------------------------- #


@pytest.mark.parametrize("key, first, last, expected", [
    ("this_week", None, None, (6, 6)),
    (None, None, None, (6, 6)),                     # this week by default
    ("next_4", None, None, (6, 9)),
    ("to_playoffs", None, None, (6, 19)),
    ("playoffs", None, None, (20, 22)),
    ("rest_of_season", None, None, (6, 22)),
    ("playoffs", 8, None, (8, 8)),                  # matchups asked for win over the preset
    (None, 3, 8, (6, 8)),                           # a window under way starts at the current matchup
    (None, None, 10, (6, 10)),
])
def test_a_window_is_a_run_of_matchups(key, first, last, expected):
    assert lv.window_range(key, first, last, current=6, regular=19, final=22) == expected


def test_next_4_stops_at_the_season_s_last_matchup():
    assert lv.window_range("next_4", None, None, current=21, regular=19, final=22) == (21, 22)


@pytest.mark.parametrize("key, first, last, current, final, code", [
    (None, 9, 8, 6, 22, "LEAGUE_VALUES_BAD_WINDOW"),
    (None, 6, 23, 6, 22, "LEAGUE_VALUES_BAD_WINDOW"),
    ("whenever", None, None, 6, 22, "LEAGUE_VALUES_BAD_WINDOW"),
    (None, 1, 3, 6, 22, "LEAGUE_VALUES_WINDOW_OVER"),
    ("to_playoffs", None, None, 20, 22, "LEAGUE_VALUES_WINDOW_OVER"),
    ("this_week", None, None, 23, 22, "LEAGUE_VALUES_WINDOW_OVER"),
    ("playoffs", None, None, 6, 19, "LEAGUE_VALUES_NO_PLAYOFFS"),
])
def test_a_window_the_league_can_t_be_priced_over_says_why(key, first, last, current, final, code):
    with pytest.raises(lv.WindowError) as err:
        lv.window_range(key, first, last, current=current, regular=19, final=final)
    assert err.value.code == code


def test_a_window_s_days_run_from_today_over_every_week_of_its_matchups():
    weeks = [full_week(6, MON), full_week(7, MON + timedelta(7)), full_week(8, MON + timedelta(14))]
    numbers, days = lv.window_days({7: (7, 8)}, weeks, 6, 7, MON + timedelta(2))
    assert numbers == [6, 7, 8]                     # matchup 7 is a two-week round
    assert days[0][0] == MON + timedelta(2) and days[-1][0] == MON + timedelta(20) and len(days) == 19
    # a week the calendar doesn't carry has no days
    assert lv.window_days({}, weeks, 9, 9, MON) == ([9], [])


def test_a_player_s_games_count_his_chance_and_only_what_s_left_of_tonight():
    days = [(MON, ALL), (MON + timedelta(1), frozenset({"BOS"})), (MON + timedelta(2), ALL)]
    back_wednesday = player("Back Wed", "ATL", 40, timeline=[Run(MON, MON + timedelta(1), 2, 0.0),
                                                             Run(MON + timedelta(2), MON + timedelta(30), 10, 0.5)])
    assert lv.expected_games(back_wednesday, days, MON, None, True) == (2, 0.5)
    starter = player("Starter", "ATL", 30, slot="C")
    benched = player("Benched", "ATL", 30, slot="BE")
    free = player("Free", "ATL", 30)
    left = {"ATL": 0.25}
    # tonight's game is a quarter left: it counts that much for the starter it locked in,
    # and not at all for a benched or free player, who can't be moved into it any more
    assert lv.expected_games(starter, days, MON, left, True) == (2, 1.25)
    assert lv.expected_games(benched, days, MON, left, True) == (1, 1.0)
    assert lv.expected_games(free, days, MON, left, False) == (1, 1.0)
    assert lv.expected_games(starter, days, MON, {"ATL": 0.0}, True) == (1, 1.0)    # over
    assert lv.expected_games(player("None", None, 30), days, MON, None, False) == (0, 0.0)


def test_equal_values_share_the_better_rank():
    assert lv.rank_all([10.0, 30.0, 20.0, 30.0]) == [4, 1, 3, 1]


# ------------------------------- the league priced ------------------------------- #


WEEKS = [full_week(6, MON), full_week(7, MON + timedelta(7)),
         week(8, MON + timedelta(14), [ALL, ALL, set(), set(), set(), ALL, ALL]),
         full_week(9, MON + timedelta(21))]


def priced_league(**over):
    """Three teams with two UT seats, matchups 6 and 7 regular, 8-9 one two-week final for
    the top two. Atlas carries a third player who only plays when a starter's team is idle."""
    rosters = (
        lp.Roster(1, "Atlas", (player("A1", "ATL", 20), player("A2", "BOS", 10),
                               player("A3", "CHI", 5, slot="BE"))),
        lp.Roster(2, "Bricks", (player("B1", "CHI", 25), player("B2", "DEN", 15))),
        lp.Roster(3, "Cookers", (player("C1", "ATL", 12, projected=False),)),
    )
    data = dict(rosters=rosters, standings=(), schedule=(), periods={8: (8, 9)}, current_matchup=6,
                regular_season_matchups=7, playoff_teams=2, roster_slots={"UT": 2, "BE": 1}, mine=1)
    data.update(over)
    return lp.League(**data)


def test_every_player_is_priced_for_the_window_and_the_season():
    hurt = player("Hurt FA", "DEN", 40, timeline=[Run(MON, MON + timedelta(6), 3, 0.0),
                                                  Run(MON + timedelta(7), MON + timedelta(60), 20, 1.0)])
    out = lv.price_league(priced_league(), [player("Streamer", "BOS", 18), hurt], WEEKS, MON + timedelta(1),
                          6, 6, "this_week")
    atlas = out["rostered"][1]
    a1, a2, a3 = atlas
    # six days left this week: A1 every one; A3 sits behind two starters who all play every day
    assert (a1.team_games, a1.games, a1.value, a1.started) == (6, 6.0, 120.0, 120.0)
    assert (a3.value, a3.started) == (30.0, 0.0)
    streamer, fa_hurt = out["free_agents"]
    assert streamer.started is None and streamer.value == 108.0
    # out this week, back next: worthless in the window, a top player for the season
    assert fa_hurt.value == 0.0 and fa_hurt.window_rank == 8 and fa_hurt.season_rank == 1
    assert a1.season_value > a1.value and a1.window_rank == 2           # B1's 150 is first
    w = out["window"]
    assert (w["first_matchup"], w["last_matchup"], w["weeks"], w["days"], w["playoffs"]) == (6, 6, [6], 6, False)
    assert w["start"] == "2026-11-24" and w["end"] == "2026-11-29" and w["key"] == "this_week"
    # the season runs through the two-week final, whose first week is missing games
    assert out["final_matchup"] == 8 and out["season_end"] == "2026-12-20"


def test_the_playoff_window_counts_both_weeks_of_a_round_and_flags_the_calendar_s_gap():
    out = lv.price_league(priced_league(), [], WEEKS, MON, 8, 8, "playoffs")
    w = out["window"]
    assert w["weeks"] == [8, 9] and w["playoffs"] and w["schedule_incomplete"] and w["incomplete_weeks"] == [8]
    a1 = out["rostered"][1][0]
    assert a1.team_games == 4 + 7 and a1.value == 20 * 11


def test_a_bench_player_starts_on_the_days_a_starter_s_team_is_idle():
    weeks = [week(6, MON, [{"ATL", "CHI"}, {"BOS", "CHI"}, ALL])]
    league = priced_league(periods={})
    out = lv.price_league(league, [], weeks, MON, 6, 6)
    a1, a2, a3 = out["rostered"][1]
    # A3 (CHI) starts Monday beside A1 and Tuesday beside A2, and sits Wednesday
    assert (a3.value, a3.started) == (15.0, 10.0)
    assert (a1.started, a2.started) == (40.0, 20.0)
    assert out["window"]["key"] is None


def test_the_tiers_set_each_roster_s_nth_best_against_the_league_s():
    t = lv.tiers({1: [100.0, 40.0, 10.0], 2: [80.0, 60.0], 3: [50.0]}, 1)
    assert t[0] == {"tier": 1, "you": 100.0, "rank": 1, "average": 76.7, "median": 80.0, "best": 100.0,
                    "worst": 50.0, "teams": 3}
    assert (t[1]["you"], t[1]["rank"], t[1]["teams"]) == (40.0, 2, 2)
    assert (t[2]["you"], t[2]["rank"], t[2]["teams"]) == (10.0, 1, 1)
    assert lv.tiers({}, 1) == [] and lv.tiers({2: [5.0]}, None)[0]["you"] is None


# ------------------------------- the service ------------------------------- #


def _league_info(provider=FantasyProvider.ESPN):
    return SimpleNamespace(provider=provider, league_id=7, year=2027)


def test_a_yahoo_team_is_told_values_are_espn_only():
    with pytest.raises(BadRequestError) as err:
        asyncio.run(lvs.league_values(_league_info(FantasyProvider.YAHOO)))
    assert err.value.error_code == "LEAGUE_PROJECTION_ESPN_ONLY"


def test_a_free_agent_list_that_doesn_t_load_is_none(monkeypatch):
    from services.espn_service import EspnService

    async def refuse(league_info, count):
        raise ProviderError("espn", "down", error_code="PROVIDER_ERROR")

    monkeypatch.setattr(EspnService, "get_free_agents", staticmethod(refuse))
    info = SimpleNamespace(league_id=7, model_copy=lambda update: SimpleNamespace(league_id=7))
    assert asyncio.run(lvs.read_free_agents(info)) is None


def test_free_agents_are_valued_off_the_live_projection_and_else_off_stored_stats(monkeypatch):
    from services import live_projection
    from services.player_value_service import PlayerValueService
    from services.providers import identity
    from services.scoring.models import StatLine

    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(
        lambda info: SimpleNamespace(points=SimpleNamespace(score=lambda line: line.pts * 1.0))))
    monkeypatch.setattr(identity, "nba_ids_by_espn_id", lambda ids: {201: 1, 204: 4, 205: 5})
    timeline = (Run(MON, MON + timedelta(60), 30, 0.8),)
    snapshot = live_projection.LiveSnapshot(as_of=MON, version="v", players={
        1: live_projection.LivePlayer(player_id=1, name="Live", espn_id=201, name_normalized="live", team="DEN",
                                      line=StatLine(pts=24.0), games=27.0, timeline=timeline)})
    fa = lambda pid, name, team, avg, injury=None, source="rolling": SimpleNamespace(
        player_id=pid, name=name, team=team, avg_points=avg, injury_status=injury, value_source=source,
        valid_positions=["PG", "G", "UT1", "UT2"])
    listed = [fa(201, "Live", "DEN", 20.0), fa(202, "Stored", "BOS", 15.0), fa(203, "Hurt", "CHI", 18.0, "OUT"),
              fa(101, "Rostered", "ATL", 30.0), fa(204, "Idle", "ORL", 42.1, source="baseline"),
              fa(205, "Debut", "MIA", 25.0)]
    info = SimpleNamespace(model_copy=lambda update: None)
    out = lvs.value_free_agents(listed, info, snapshot, {101})
    assert [f.name for f, _ in out] == ["Live", "Stored", "Hurt", "Idle", "Debut"]   # ESPN's stale rostered one left out
    live, stored, hurt, idle, debut = (p for _, p in out)
    assert (live.value, live.team, live.timeline, live.projected, live.player_id) == (24.0, "DEN", timeline, True, 1)
    assert live.slots == {"PG", "G", "UT"} and lv.positions(live.slots) == ["PG"]
    assert (stored.value, stored.chance, stored.projected, stored.unplayed) == (15.0, 1.0, False, False)
    assert (hurt.chance, hurt.unplayed) == (0.0, False)
    # the projection carries everyone who has played: one it leaves out, valued off last season, hasn't
    assert (idle.value, idle.chance, idle.projected, idle.unplayed) == (42.1, 0.0, False, True)
    # ...unless our own games have him playing lately (a debut since the snapshot)
    assert (debut.chance, debut.unplayed) == (1.0, False)
    # with no snapshot this morning there is nothing to tell by: stored stats, every game
    idle = next(p for f, p in lvs.value_free_agents(listed, info, None, {101}) if f.name == "Idle")
    assert (idle.chance, idle.unplayed) == (1.0, False)


def _fake_reads(monkeypatch, listed, today_left=None):
    from services.espn_service import EspnService
    from services.lineup_read_service import ParsedEntry

    payload = {"settings": {"name": "Dreamers", "scheduleSettings": {"matchupPeriodCount": 7, "playoffTeamCount": 2,
                                                                     "matchupPeriods": {"8": [8, 9]}},
                            "rosterSettings": {"lineupSlotCounts": {"11": 2, "12": 1}}},
               "status": {"currentMatchupPeriod": 6}, "teams": [{"id": 1}]}

    async def read(league_info):
        return payload

    async def free(league_info, count=lvs.FREE_AGENTS_READ):
        return listed

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def left(today):
        return today_left

    league = priced_league()
    entries = [(r.league_team_id, r.team_name,
                tuple(ParsedEntry(100 * r.league_team_id + i, p.name, p.team, 0, (11, 12), False,
                                  "ACTIVE" if i else "DAY_TO_DAY", False)
                      for i, p in enumerate(r.players)))
               for r in league.rosters]
    monkeypatch.setattr(lps, "read_points_league", read)
    monkeypatch.setattr(lvs, "read_free_agents", free)
    monkeypatch.setattr(EspnService, "resolve_team", staticmethod(lambda info, teams: {"id": 1}))
    monkeypatch.setattr(lps, "parse_inputs", lambda payload, mine: ([], [], entries))
    monkeypatch.setattr(lps, "value_players", lambda rosters, info, today: (league.rosters, None))
    monkeypatch.setattr(lps, "dated_calendar", lambda: tuple(WEEKS))
    monkeypatch.setattr(lps, "games_left_today", left)
    monkeypatch.setattr(lvs, "run_db", direct)
    monkeypatch.setattr(lvs, "run_cpu", direct)
    monkeypatch.setattr(lvs, "value_free_agents", lambda fas, info, snap, rostered: [
        (f, player(f.name, f.team, f.avg_points, slots=("PG", "UT"), projected=False)) for f in fas])


def _fa(pid, name, team, avg, status="free_agent"):
    return SimpleNamespace(player_id=pid, name=name, team=team, avg_points=avg, injury_status=None,
                           acquisition_status=status)


def test_the_service_prices_the_rosters_and_the_best_free_agents(monkeypatch):
    listed = [_fa(901, "Streamer", "BOS", 18.0), _fa(902, "Deep", "CHI", 2.0, "waivers"),
              _fa(903, "Mid", "DEN", 9.0)]
    _fake_reads(monkeypatch, listed)
    out = asyncio.run(lvs.league_values(_league_info(), window="next_4", free_agents=2, today=MON + timedelta(1)))
    LeagueValuesData.model_validate(out)
    names = [p["name"] for p in out["players"]]
    assert names[0] == "B1" and "Deep" not in names and {"Streamer", "Mid"} <= set(names)   # the best two FAs
    assert [p["window_rank"] for p in out["players"]] == sorted(p["window_rank"] for p in out["players"])
    a1 = next(p for p in out["players"] if p["name"] == "A1")
    assert (a1["mine"], a1["league_team_id"], a1["team_name"], a1["espn_id"], a1["injury_status"]) == (
        True, 1, "Atlas", 100, "DAY_TO_DAY")
    streamer = next(p for p in out["players"] if p["name"] == "Streamer")
    assert (streamer["league_team_id"], streamer["availability"], streamer["started"]) == (None, "free_agent", None)
    assert streamer["positions"] == ["PG"]
    # next 4 runs into the final, so through matchup 8 (weeks 8 and 9); every player ranks among 6 + 3
    w = out["window"]
    assert (w["key"], w["first_matchup"], w["last_matchup"], w["weeks"], w["playoffs"]) == (
        "next_4", 6, 8, [6, 7, 8, 9], True)
    assert out["ranked_players"] == 9 and out["free_agents_read"] == 3 and out["final_matchup"] == 8
    assert out["tiers"][0]["you"] == a1["value"]
    assert any(n.startswith("The calendar is missing games in week 8") for n in out["notes"])
    assert any(n.startswith("No live projection this morning") for n in out["notes"])


def test_a_free_agent_who_hasn_t_played_counts_for_no_games_and_is_named(monkeypatch):
    listed = [_fa(901, "Streamer", "BOS", 18.0), _fa(902, "Idle", "ORL", 42.1)]
    _fake_reads(monkeypatch, listed)
    monkeypatch.setattr(lvs, "value_free_agents", lambda fas, info, snap, rostered: [
        (f, player(f.name, f.team, f.avg_points, slots=("PG", "UT"), projected=False,
                   chance=0.0 if f.name == "Idle" else 1.0, unplayed=f.name == "Idle")) for f in fas])
    out = asyncio.run(lvs.league_values(_league_info(), window="next_4", today=MON + timedelta(1)))
    idle = next(p for p in out["players"] if p["name"] == "Idle")
    assert (idle["games"], idle["value"], idle["projected"]) == (0.0, 0.0, False)
    assert idle["window_rank"] == max(p["window_rank"] for p in out["players"])
    assert "Idle hasn't played this season, so he counts for no games." in out["notes"]


def test_the_notes_count_the_unprojected_apart_from_the_unplayed():
    result = {"window": {"incomplete_weeks": []}}
    assert lvs.notes_for(result, MON, 2, 150, 1, ["Kevin Porter Jr."]) == [
        "2 players had no live projection and are valued off stored stats.",
        "Kevin Porter Jr. hasn't played this season, so he counts for no games."]
    assert lvs.notes_for(result, MON, 0, 150, 1, ["A", "B"]) == [
        "A and B haven't played this season, so they count for no games."]
    assert lvs.notes_for(result, MON, 0, 150, 1, ["A", "B", "C", "D", "E"])[0].startswith("A, B, C and 2 more haven't")
    assert lvs.notes_for(result, MON, 0, 150, 1) == []


def test_the_service_refuses_a_window_with_its_code_and_prices_rosters_alone_without_free_agents(monkeypatch):
    _fake_reads(monkeypatch, None)
    with pytest.raises(BadRequestError) as err:
        asyncio.run(lvs.league_values(_league_info(), first_matchup=2, last_matchup=3, today=MON))
    assert err.value.error_code == "LEAGUE_VALUES_WINDOW_OVER" and err.value.status_code == 400
    out = asyncio.run(lvs.league_values(_league_info(), first_matchup=7, today=MON))
    assert out["window"]["key"] is None and (out["window"]["first_matchup"], out["window"]["last_matchup"]) == (7, 7)
    assert out["free_agents_read"] is None and out["ranked_players"] == 6
    assert all(p["league_team_id"] is not None for p in out["players"])
    assert any(n.startswith("ESPN's free agent list didn't load") for n in out["notes"])


def test_tonight_s_games_under_way_reach_the_pricing(monkeypatch):
    _fake_reads(monkeypatch, [], today_left={"ATL": 0.0})
    out = asyncio.run(lvs.league_values(_league_info(), today=MON))
    a1 = next(p for p in out["players"] if p["name"] == "A1")
    assert a1["team_games"] == 6                    # ATL's Monday game is over
