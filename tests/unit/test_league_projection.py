"""League projections (services/league_projection.py and its service): every roster's days
seated in the league's slots, the benchmark rows, the calendar's gaps, the bracket and the
simulated odds, and the service's reads around them."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.errors import BadRequestError
from schemas.common import FantasyProvider, LeagueInfo
from schemas.league_projection import LeagueProjectionData
from services import league_projection as lp
from services import league_projection_service as lps
from services.live_projection import Run

pytestmark = pytest.mark.unit

MON = date(2026, 11, 23)
ALL = frozenset({"ATL", "BOS", "CHI", "DEN"})


def week(number, start, days):
    return lp.DatedWeek(number=number, start=start, days=tuple(frozenset(d) for d in days))


def full_week(number, start, teams=ALL):
    return week(number, start, [teams] * 7)


def player(name, team, value, slots=("UT",), chance=1.0, timeline=(), projected=True):
    return lp.ProjPlayer(name=name, team=team, value=value, slots=frozenset(slots), chance=chance,
                         timeline=tuple(timeline), projected=projected)


# ------------------------------- the calendar ------------------------------- #


def test_a_week_missing_games_is_incomplete_but_a_quiet_night_or_the_all_star_break_is_not():
    thanksgiving = week(5, MON, [ALL, ALL, ALL, set(), ALL, ALL, ALL])
    cup = week(7, MON + timedelta(7), [ALL, ALL, ALL, ALL, set(), set(), set()])
    all_star = week(18, MON + timedelta(14), [ALL] * 4 + [set()] * 6 + [ALL] * 4)    # 14 days
    assert lp.incomplete_weeks([thanksgiving, cup, all_star]) == {7}


def test_a_matchup_without_a_period_map_is_its_own_week():
    assert lp.matchup_weeks({20: (20, 21)}, 20) == (20, 21)
    assert lp.matchup_weeks({}, 9) == (9,)


# ------------------------------- a roster's day ------------------------------- #


def test_a_day_seats_the_best_players_its_slots_can_hold():
    big1 = player("Big One", "ATL", 40, slots=("C", "UT"))
    big2 = player("Big Two", "BOS", 30, slots=("C", "UT"))
    big3 = player("Big Three", "CHI", 20, slots=("C", "UT"))
    guard = player("Guard", "DEN", 25, slots=("PG", "UT"))
    # one C and one UT: the two best centres start, the guard's PG seat isn't in the league
    assert lp.day_value([big1, big2, big3, guard], ALL, MON, ["C", "UT"]) == 70
    # with a PG seat the guard starts too
    assert lp.day_value([big1, big2, big3, guard], ALL, MON, ["C", "UT", "PG"]) == 95


def test_a_player_counts_at_his_chance_to_play_and_only_on_his_team_s_nights():
    healthy = player("Healthy", "ATL", 30)
    back_wednesday = player("Back Wed", "BOS", 40, timeline=[Run(MON, MON + timedelta(1), 2, 0.0),
                                                             Run(MON + timedelta(2), MON + timedelta(30), 10, 0.5)])
    no_team = player("Free", None, 50)
    assert lp.day_value([healthy, back_wednesday, no_team], ALL, MON, ["UT", "UT"]) == 30
    assert lp.day_value([healthy, back_wednesday, no_team], ALL, MON + timedelta(2), ["UT", "UT"]) == 50
    assert lp.day_value([healthy], frozenset({"BOS"}), MON, ["UT"]) == 0


def test_once_a_game_starts_its_player_holds_his_slot_and_adds_only_the_rest_of_it():
    started = lp.ProjPlayer(name="Started", team="ATL", value=40, slots=frozenset({"C", "UT"}), slot="C")
    sat = lp.ProjPlayer(name="Sat", team="BOS", value=30, slots=frozenset({"C", "UT"}), slot="BE")
    later = lp.ProjPlayer(name="Later", team="CHI", value=20, slots=frozenset({"C", "UT"}), slot="BE")
    left = {"ATL": 0.25, "BOS": 0.0, "CHI": 1.0}
    # ATL is three quarters played: the centre keeps the C seat and adds a quarter of his 40;
    # BOS is over and its player sat; CHI is still to play and takes the UT seat
    assert lp.day_value([started, sat, later], ALL, MON, ["C", "UT"], left) == 10 + 20
    # read whole, with no word on tonight, the best two are seated as before
    assert lp.day_value([started, sat, later], ALL, MON, ["C", "UT"]) == 70
    # a centre locked in the C seat leaves none for a centre still to play
    late_big = lp.ProjPlayer(name="Late Big", team="CHI", value=50, slots=frozenset({"C"}), slot="BE")
    assert lp.day_value([started, late_big], ALL, MON, ["C", "UT"], {"ATL": 0.0, "CHI": 1.0}) == 0


def test_a_roster_s_weeks_count_from_the_first_day():
    roster = lp.Roster(1, "Atlas", (player("A", "ATL", 10),))
    weeks = [full_week(6, MON), full_week(7, MON + timedelta(7))]
    assert lp.week_values(roster, weeks, MON + timedelta(3), ["UT"]) == {6: 40, 7: 70}


# ------------------------------- benchmarks ------------------------------- #


def test_a_benchmark_reads_one_team_against_the_league():
    values = {1: 100.0, 2: 120.0, 3: 90.0, 4: 110.0, 5: 80.0}
    b = lp.benchmark(values, 1, 3)
    assert (b["you"], b["rank"], b["median"], b["line"], b["best"], b["worst"], b["teams"]) == (
        100.0, 3, 100.0, 100.0, 120.0, 80.0, 5)
    assert lp.benchmark({}, 1, 3)["you"] is None
    assert lp.benchmark(values, None, 0)["line"] is None and lp.benchmark(values, None, 0)["rank"] is None


def test_tied_teams_share_the_better_rank():
    assert lp.ranks({1: 10.0, 2: 12.0, 3: 12.0}) == {1: 3, 2: 1, 3: 1}


def test_an_even_matchup_is_a_coin_flip_and_a_lead_already_scored_counts():
    assert lp.win_chance(0, 1000, 0, 1000, 1000) == pytest.approx(0.5)
    banked = lp.win_chance(300, 800, 0, 1000, 1000)      # 100 ahead, 300 of it already scored
    to_play = lp.win_chance(0, 1100, 0, 1000, 1000)      # 100 ahead, all of it still to play
    assert 0.5 < to_play < banked
    assert lp.win_chance(0, 0, 0, 0, 1000) == 0.5


# ------------------------------- the bracket and the odds ------------------------------- #


def test_the_bracket_keeps_the_top_two_seeds_apart_until_the_final():
    assert lp.bracket_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    assert lp.bracket_order(4) == [1, 4, 2, 3]
    assert [lp.playoff_rounds(n) for n in (0, 1, 2, 4, 6, 8)] == [0, 0, 1, 2, 3, 3]


def standing(tid, w, l, pf, rank=None):
    return lp.Standing(tid, f"Team {tid}", rank or tid, w, l, 0, pf, 0.0)


def four_team_sim(means, runs=4000, playoff_teams=2, standings=None):
    ids = [1, 2, 3, 4]
    st = standings or {t: standing(t, 2, 2, 400.0) for t in ids}
    games = [(5, 1, 2), (5, 3, 4), (6, 1, 3), (6, 2, 4)]
    mean = {(t, m): means[t] for t in ids for m in (5, 6, 7)}
    return lp.simulate(ids, st, games, {}, mean, [7], playoff_teams, 100.0, runs=runs, seed=3)


def test_the_simulation_s_odds_add_up_and_favour_the_stronger_roster():
    odds = four_team_sim({1: 140.0, 2: 100.0, 3: 100.0, 4: 60.0})
    assert sum(odds.playoffs.values()) == pytest.approx(2.0)
    assert sum(odds.title.values()) == pytest.approx(1.0)
    for t in (1, 2, 3, 4):
        assert sum(odds.seeds[t]) == pytest.approx(1.0)
    assert odds.playoffs[1] > 0.95 and odds.playoffs[4] < 0.05
    assert odds.title[1] > odds.title[2] > odds.title[4]
    assert odds.wins[1] > odds.wins[4]


def test_the_same_seed_plays_the_same_seasons():
    a = four_team_sim({1: 110.0, 2: 100.0, 3: 100.0, 4: 90.0}, runs=500)
    b = four_team_sim({1: 110.0, 2: 100.0, 3: 100.0, 4: 90.0}, runs=500)
    assert a.playoffs == b.playoffs and a.title == b.title


def test_a_record_already_out_of_reach_decides_the_seed_whatever_the_roster():
    st = {1: standing(1, 9, 0, 900.0), 2: standing(2, 0, 9, 900.0), 3: standing(3, 5, 4, 800.0),
          4: standing(4, 4, 5, 800.0)}
    odds = four_team_sim({1: 50.0, 2: 200.0, 3: 100.0, 4: 100.0}, standings=st)
    assert odds.playoffs[1] == 1.0 and odds.seeds[1][0] == 1.0
    assert odds.playoffs[2] == 0.0


def test_records_seed_the_field_and_points_for_breaks_a_tie():
    ids = [1, 2, 3, 4]
    st = {1: standing(1, 6, 3, 100.0), 2: standing(2, 6, 3, 5000.0), 3: standing(3, 9, 0, 50.0),
          4: standing(4, 0, 9, 9000.0)}
    mean = {(t, 7): 100.0 for t in ids}
    odds = lp.simulate(ids, st, [], {}, mean, [7], 2, 100.0, runs=500, seed=3)
    assert odds.seeds[3][0] == 1.0                     # the best record, whatever its points
    assert odds.seeds[2][1] == 1.0                     # 6-3 like team 1, with more points for
    assert odds.playoffs[1] == 0.0 and odds.playoffs[4] == 0.0


def test_a_six_team_field_gives_the_top_two_seeds_a_bye():
    ids = [1, 2, 3, 4, 5, 6]
    st = {t: standing(t, 20 - t, t, 1000.0) for t in ids}          # seeds 1..6 locked in
    mean = {(t, m): 100.0 for t in ids for m in (20, 21, 22)}
    odds = lp.simulate(ids, st, [], {}, mean, [20, 21, 22], 6, 100.0, runs=6000, seed=1)
    assert all(odds.playoffs[t] == 1.0 for t in ids)
    # a bye is one game fewer to win: 1/4 against 1/8 for even rosters
    assert odds.title[1] == pytest.approx(0.25, abs=0.03)
    assert odds.title[3] == pytest.approx(0.125, abs=0.03)


def test_a_reseeding_league_sends_seed_1_against_the_lowest_seed_left():
    ids = [1, 2, 3, 4, 5, 6]
    st = {t: standing(t, 20 - t, t, 1000.0) for t in ids}          # seeds 1..6 locked in
    # 6 beats 3, 4 and 5 are a coin flip, and 1, 2 and 6 are even
    strength = {1: 100.0, 2: 100.0, 3: 1.0, 4: 1.0, 5: 1.0, 6: 100.0}
    mean = {(t, m): strength[t] for t in ids for m in (20, 21, 22)}

    def title(reseed):
        return lp.simulate(ids, st, [], {}, mean, [20, 21, 22], 6, 100.0, runs=6000, seed=2,
                           reseed=reseed).title

    # fixed bracket: 1 meets the 4-5 winner and is in the final; 2 and 6 split the other side
    assert title(False)[1] == pytest.approx(0.5, abs=0.03)
    # reseeded: 1 meets 6 (the lowest seed left), and 2 gets the 4-5 winner instead
    assert title(True)[1] == pytest.approx(0.25, abs=0.03)
    assert title(True)[2] == pytest.approx(0.5, abs=0.03)


# ------------------------------- the whole projection ------------------------------- #


def small_league(**over):
    """Four teams, three regular-season matchups, two make it, one one-week final.
    Matchup 1 is final, 2 is under way (Monday of week 2), 3 and the final are to come;
    week 3 is missing games."""
    rosters = (
        lp.Roster(1, "Atlas", (player("A1", "ATL", 20), player("A2", "BOS", 10))),
        lp.Roster(2, "Bricks", (player("B1", "CHI", 25), player("B2", "DEN", 10))),
        lp.Roster(3, "Cookers", (player("C1", "ATL", 15), player("C2", "BOS", 5, projected=False))),
        lp.Roster(4, "Dunkers", (player("D1", "CHI", 10),)),
    )
    standings = (standing(1, 1, 0, 300.0, 1), standing(3, 1, 0, 250.0, 2), standing(2, 0, 1, 280.0, 3),
                 standing(4, 0, 1, 100.0, 4))
    schedule = (
        lp.Pairing(1, 1, 2, 300.0, 280.0, "home"), lp.Pairing(1, 3, 4, 250.0, 100.0, "home"),
        lp.Pairing(2, 1, 3, 40.0, 20.0), lp.Pairing(2, 2, 4, 30.0, 10.0),
        lp.Pairing(3, 4, 1), lp.Pairing(3, 2, 3),
    )
    data = dict(rosters=rosters, standings=standings, schedule=schedule, periods={4: (4,)},
                current_matchup=2, regular_season_matchups=3, playoff_teams=2,
                roster_slots={"UT": 2, "BE": 1}, mine=1)
    data.update(over)
    return lp.League(**data)


WEEK1 = MON - timedelta(7)
WEEKS = [full_week(1, WEEK1), full_week(2, MON),
         week(3, MON + timedelta(7), [ALL, ALL, set(), set(), set(), ALL, ALL]),
         full_week(4, MON + timedelta(14))]


def test_the_projection_reads_finals_as_scored_and_the_rest_as_projected():
    out = lp.project(small_league(), WEEKS, MON + timedelta(1), runs=2000, seed=5)
    rows = {m["matchup"]: m for m in out["matchups"]}
    assert [m["status"] for m in out["matchups"]] == ["final", "current", "upcoming", "upcoming"]
    # final: the scores as played, my result and opponent
    assert rows[1]["you"] == 300.0 and rows[1]["best"] == 300.0 and rows[1]["result"] == "W"
    assert rows[1]["opponent"] == "Bricks" and rows[1]["opponent_score"] == 280.0
    # current: 40 so far plus six days of 30
    assert rows[2]["you"] == 40.0 + 6 * 30 and rows[2]["opponent"] == "Cookers"
    assert rows[2]["win_chance"] > 0.95                          # 220 against 20 + 6 x 20
    # week 3 is flagged, the final is in the playoffs
    assert rows[3]["schedule_incomplete"] and not rows[4]["schedule_incomplete"]
    assert rows[4]["playoffs"] and out["playoff_matchups"] == [4] and out["playoff_weeks"] == [4]
    assert out["incomplete_weeks"] == [3]


def test_with_tonight_read_live_the_current_matchup_adds_only_what_s_left_of_it():
    last_day = MON + timedelta(6)
    # every game tonight is over: the live score already holds them, so it stands
    over = lp.project(small_league(today_left={t: 0.0 for t in ALL}), WEEKS, last_day, runs=200, seed=1)
    assert {m["matchup"]: m for m in over["matchups"]}[2]["you"] == 40.0
    # half of tonight still to play: half of Atlas's 30
    half = lp.project(small_league(today_left={t: 0.5 for t in ALL}), WEEKS, last_day, runs=200, seed=1)
    assert {m["matchup"]: m for m in half["matchups"]}[2]["you"] == 40.0 + 15
    # with the settled score, today counts whole
    whole = lp.project(small_league(), WEEKS, last_day, runs=200, seed=1)
    assert {m["matchup"]: m for m in whole["matchups"]}[2]["you"] == 40.0 + 30


def test_the_per_week_averages_leave_out_the_calendar_s_gaps_and_the_week_under_way():
    out = lp.project(small_league(), WEEKS, MON + timedelta(1), runs=200, seed=5)
    atlas = next(t for t in out["teams"] if t["league_team_id"] == 1)
    # week 3 is incomplete and week 2 began before today: no whole regular-season week is left
    assert atlas["regular_season_week"] is None
    assert atlas["playoff_week"] == 7 * 30 and atlas["playoff_rank"] == 2
    assert out["benchmarks"]["playoffs"]["you"] == 210.0
    assert out["benchmarks"]["playoffs"]["best"] == 7 * 35


def test_the_projection_names_the_unprojected_and_sorts_by_the_standings():
    out = lp.project(small_league(), WEEKS, MON + timedelta(1), runs=200, seed=5)
    assert [t["league_team_id"] for t in out["teams"]] == [1, 3, 2, 4]
    cookers = next(t for t in out["teams"] if t["league_team_id"] == 3)
    assert cookers["players_unprojected"] == ["C2"] and cookers["players_projected"] == 1
    assert sum(t["playoff_odds"] for t in out["teams"]) == pytest.approx(2.0)
    assert sum(t["title_odds"] for t in out["teams"]) == pytest.approx(1.0)


def test_no_odds_once_the_playoffs_have_started():
    out = lp.project(small_league(current_matchup=4), WEEKS, MON + timedelta(15), runs=200)
    assert out["in_playoffs"] and out["simulations"] == 0
    assert all(t["playoff_odds"] is None for t in out["teams"])


def test_the_result_fits_the_response_schema():
    out = lp.project(small_league(), WEEKS, MON + timedelta(1), runs=200, seed=5)
    LeagueProjectionData(league_name="L", format="points", today="2026-11-24", teams_count=4, playoff_teams=2,
                         seed_tiebreaker="points_for", lineup_slots={"UT": 2}, notes=[], **out)


# ------------------------------- the service ------------------------------- #


def test_the_league_s_slots_and_periods_are_read_off_its_settings():
    payload = {"settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "4": 1, "5": 1, "11": 3, "12": 3, "13": 1,
                                                                    "14": 0}},
                            "scheduleSettings": {"matchupPeriods": {"1": [1], "20": [20, 21], "x": [3], "5": []}}}}
    assert lps.roster_slots(payload) == {"PG": 1, "C": 1, "G": 1, "UT": 3, "BE": 3, "IR": 1}
    assert lps.matchup_periods(payload) == {1: (1,), 20: (20, 21)}


def _league_info(provider=FantasyProvider.ESPN):
    return SimpleNamespace(provider=provider, league_id=7, year=2027)


def test_a_yahoo_team_is_told_projections_are_espn_only():
    with pytest.raises(BadRequestError) as err:
        asyncio.run(lps.league_projection(_league_info(FantasyProvider.YAHOO)))
    assert err.value.error_code == "LEAGUE_PROJECTION_ESPN_ONLY" and err.value.status_code == 400


def test_a_category_league_is_told_projections_are_points_only(monkeypatch):
    from services.espn_service import EspnService

    async def fetch(league_info, views):
        return {"settings": {"scoringSettings": {"scoringType": "H2H_CATEGORY"}}, "teams": []}

    monkeypatch.setattr(EspnService, "fetch_league", staticmethod(fetch))
    with pytest.raises(BadRequestError) as err:
        asyncio.run(lps.league_projection(_league_info(), today=MON))
    assert err.value.error_code == "LEAGUE_PROJECTION_POINTS_ONLY"


def test_without_a_calendar_the_league_is_not_projected(monkeypatch):
    from core.errors import ServiceUnavailableError
    from services import schedule_service
    from services.espn_service import EspnService

    def no_calendar():
        raise FileNotFoundError("matchupsPerDay")

    monkeypatch.setattr(schedule_service, "iter_weeks", no_calendar)
    assert lps.dated_calendar() is None                # not the same as a season with no weeks left

    async def fetch(league_info, views):
        return {"settings": {"scoringSettings": {"scoringType": "H2H_POINTS"}}, "teams": [], "status": {}}

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(EspnService, "fetch_league", staticmethod(fetch))
    monkeypatch.setattr(EspnService, "resolve_team", staticmethod(lambda info, teams: None))
    monkeypatch.setattr(lps, "run_db", direct)
    monkeypatch.setattr(lps, "parse_inputs", lambda payload, mine: ([], [], []))
    monkeypatch.setattr(lps, "value_players", lambda rosters, info, today: ((), None))
    with pytest.raises(ServiceUnavailableError) as err:
        asyncio.run(lps.league_projection(_league_info(), today=MON))
    assert err.value.error_code == "LEAGUE_PROJECTION_NO_CALENDAR" and err.value.status_code == 503


def test_the_current_matchup_starts_from_espn_s_settled_score_not_the_live_one():
    payload = {"status": {"currentMatchupPeriod": 6}, "schedule": [
        {"matchupPeriodId": 5, "home": {"teamId": 1, "totalPoints": 900.0}, "away": {"teamId": 2, "totalPoints": 800.0}},
        {"matchupPeriodId": 6, "home": {"teamId": 1, "totalPoints": 412.34, "totalPointsLive": 470.0},
         "away": {"teamId": 2, "totalPoints": 380.0, "totalPointsLive": 391.0}},
    ]}
    # the live total already counts today's games, so it's read apart, beside them
    assert lps.settled_scores(payload) == {1: 412.3, 2: 380.0}
    assert lps.live_scores(payload) == {1: 470.0, 2: 391.0}
    # no settled score is no score, never the live one
    del payload["schedule"][1]["away"]["totalPoints"]
    assert lps.settled_scores(payload) == {1: 412.3}


def test_without_a_settled_score_the_current_matchup_never_takes_the_live_one(monkeypatch):
    from services import league_overview, lineup_read_service
    monkeypatch.setattr(league_overview, "standings", lambda payload, mine, categories: [
        {"league_team_id": t, "team_name": f"T{t}", "rank": t, "wins": 0, "losses": 0, "ties": 0} for t in (1, 2)])
    monkeypatch.setattr(lineup_read_service, "parse_espn_lineup",
                        lambda payload, team_name, espn_team_id: SimpleNamespace(entries=()))
    payload = {"status": {"currentMatchupPeriod": 6}, "schedule": [
        {"matchupPeriodId": 6, "home": {"teamId": 1, "totalPoints": 400.0, "totalPointsLive": 450.0},
         "away": {"teamId": 2, "totalPointsLive": 391.0}}]}
    _, pairings, _ = lps.parse_inputs(payload, 1)
    assert (pairings[0].home_score, pairings[0].away_score) == (400.0, None)

    # and a side without its score so far stops the projection rather than skew the matchup and the odds
    from core.errors import ProviderError
    from services.espn_service import EspnService

    async def fetch(league_info, views):
        return {**payload, "settings": {"scoringSettings": {"scoringType": "H2H_POINTS"}}, "teams": []}

    monkeypatch.setattr(EspnService, "fetch_league", staticmethod(fetch))
    monkeypatch.setattr(EspnService, "resolve_team", staticmethod(lambda info, teams: None))
    with pytest.raises(ProviderError) as err:
        asyncio.run(lps.league_projection(_league_info(), today=MON))
    assert err.value.error_code == "LEAGUE_PROJECTION_NO_SETTLED_SCORE" and err.value.status_code == 502


def test_players_are_valued_off_the_live_projection_and_else_off_stored_stats(monkeypatch):
    from services import live_projection
    from services.espn_service import EspnService
    from services.lineup_read_service import ParsedEntry
    from services.player_value_service import PlayerValueService, ValueResult
    from services.providers import identity
    from services.scoring.models import StatLine

    weights = SimpleNamespace(points=SimpleNamespace(score=lambda line: line.pts * 1.0))
    previews = []

    def scoring_for(info):
        previews.append(info.scoring_preview)
        return weights

    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(scoring_for))
    monkeypatch.setattr(identity, "nba_ids_by_espn_id", lambda ids: {101: 1, 102: 2})
    timeline = (Run(MON, MON + timedelta(60), 30, 0.9),)
    snapshot = live_projection.LiveSnapshot(as_of=MON, version="v", players={
        1: live_projection.LivePlayer(player_id=1, name="Live", espn_id=101, name_normalized="live", team="DEN",
                                      line=StatLine(pts=30.0), games=27.0, timeline=timeline)})
    monkeypatch.setattr(live_projection, "load", lambda season, day: snapshot)
    asked = []

    def stored(scoring, ids):
        asked.extend(ids)
        return {102: ValueResult(value=22.0, source="rolling"), 103: ValueResult(value=18.0, source="rolling")}

    monkeypatch.setattr(EspnService, "_values_for", staticmethod(stored))
    entries = (
        ParsedEntry(101, "Live", "DEN", 0, (0, 5, 11, 12), False, None, False),
        ParsedEntry(102, "Stored", "BOS", 0, (4, 11, 12), False, None, False),
        ParsedEntry(103, "Hurt", "FA", 13, (2, 6, 11), True, "OUT", False),
    )
    shown_as_categories = LeagueInfo(provider="espn", league_id=7, team_name="Atlas", year=2027,
                                     scoring_preview="categories")
    rosters, snap = lps.value_players([(1, "Atlas", entries)], shown_as_categories, MON)
    live, kept, hurt = rosters[0].players
    assert previews == [None]                    # the league's own points, not the team's preview
    assert snap is snapshot and sorted(asked) == [102, 103]
    assert (live.value, live.team, live.timeline, live.projected) == (30.0, "DEN", timeline, True)
    assert live.slots == {"PG", "G", "UT", "BE"}
    assert (kept.value, kept.chance, kept.projected, kept.team) == (22.0, 1.0, False, "BOS")
    assert (hurt.chance, hurt.team) == (0.0, None)


def test_the_notes_name_the_calendar_s_gaps_and_the_unprojected():
    result = {"incomplete_weeks": [7, 8], "in_playoffs": False,
              "matchups": [{"weeks": [7], "status": "upcoming"}, {"weeks": [8], "status": "upcoming"}],
              "teams": [{"players_unprojected": ["X"]}, {"players_unprojected": []}]}
    notes = lps.notes_for(result, MON, mine=1)
    assert notes[0].startswith("The calendar is missing games in weeks 7 and 8")
    assert notes[1] == "1 rostered player had no live projection and is valued off stored stats."
    assert lps.notes_for(result, None, mine=None)[1].startswith("No live projection this morning")
    assert lps.notes_for(result, None, mine=None)[-1].startswith("Court Vision could not tell")
    assert lps.notes_for(result, MON, mine=1, divisions=2)[-1].startswith("This league has 2 divisions")
    assert not any("division" in n for n in lps.notes_for(result, MON, mine=1))
    assert lps.notes_for(result, MON, mine=1, tiebreaker="head_to_head")[-1].endswith("break them by points for.")


# ------------------------------- tonight, read live ------------------------------- #


def test_a_game_s_share_still_to_play_reads_its_status_and_clock():
    at = lambda h, m=0: lps.EASTERN.localize(datetime(2026, 11, 22, h, m))
    tip, now = at(19, 30), at(21)
    assert lps.game_left("final", 4, "PT00M00.00S", tip, now) == 0.0
    assert lps.game_left("scheduled", None, None, at(22), now) == 1.0
    assert lps.game_left("in_progress", 3, "PT06M00.00S", tip, now) == pytest.approx((720 + 360) / 2880)
    assert lps.game_left("in_progress", 2, "PT00M00.00S", tip, now) == 0.5          # halftime
    assert lps.game_left("in_progress", 5, "PT02M30.00S", tip, now) == pytest.approx(150 / 2880)
    # a live read that hasn't caught up is guessed from the tip, and a game long over never counts again
    assert lps.game_left("scheduled", None, None, tip, now) == pytest.approx(1 - 90 / 135)
    assert lps.game_left("in_progress", None, None, at(16), now) == 0.0


def test_today_s_games_give_each_team_its_share_still_to_play():
    now = lps.EASTERN.localize(datetime(2026, 11, 22, 21, 0))
    game = lambda home, away, status, start, period=None, clock=None: SimpleNamespace(
        game_date="2026-11-22", home_team=home, away_team=away, status=status, period=period,
        game_clock=clock, start_time_et=start)
    games = [game("ATL", "BOS", "final", "19:00", 4, "PT00M00.00S"), game("CHI", "DEN", "scheduled", "22:00")]
    assert lps.today_left(games, now) == {"ATL": 0.0, "BOS": 0.0, "CHI": 1.0, "DEN": 1.0}


def test_the_current_matchup_takes_the_live_score_only_when_every_side_has_one():
    pairings = [lp.Pairing(5, 1, 2, 900.0, 800.0, "home"), lp.Pairing(6, 1, 2, 412.3, 380.0),
                lp.Pairing(6, 3, None, 100.0)]
    out = lps.with_live_scores(pairings, {1: 470.0, 2: 391.0, 3: 120.0}, 6)
    assert [(p.home_score, p.away_score) for p in out] == [(900.0, 800.0), (470.0, 391.0), (120.0, None)]
    assert lps.with_live_scores(pairings, {1: 470.0, 3: 120.0}, 6) is None


@pytest.mark.parametrize("tonight, scores, left", [
    ({"ATL": 0.0}, (450.0, 391.0), {"ATL": 0.0}),       # tonight read: the live score, what's left
    (None, (400.0, 380.0), None),                        # no word on tonight: settled, today whole
])
def test_the_projection_starts_from_the_live_score_beside_tonight_s_games(monkeypatch, tonight, scores, left):
    from services import league_overview, lineup_read_service
    from services.espn_service import EspnService

    payload = {"status": {"currentMatchupPeriod": 6}, "teams": [],
               "settings": {"scoringSettings": {"scoringType": "H2H_POINTS"}},
               "schedule": [{"matchupPeriodId": 6, "home": {"teamId": 1, "totalPoints": 400.0, "totalPointsLive": 450.0},
                             "away": {"teamId": 2, "totalPoints": 380.0, "totalPointsLive": 391.0}}]}

    async def fetch(league_info, views):
        return payload

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def games(today):
        return tonight

    seen = {}

    async def capture(name, fn, league, weeks, today, **kwargs):
        seen["league"] = league
        return {"simulations": 0, "incomplete_weeks": [], "matchups": [], "teams": [], "in_playoffs": False}

    monkeypatch.setattr(league_overview, "standings", lambda payload, mine, categories: [
        {"league_team_id": t, "team_name": f"T{t}", "rank": t, "wins": 0, "losses": 0, "ties": 0} for t in (1, 2)])
    monkeypatch.setattr(lineup_read_service, "parse_espn_lineup",
                        lambda payload, team_name, espn_team_id: SimpleNamespace(entries=()))
    monkeypatch.setattr(EspnService, "fetch_league", staticmethod(fetch))
    monkeypatch.setattr(EspnService, "resolve_team", staticmethod(lambda info, teams: None))
    monkeypatch.setattr(lps, "run_db", direct)
    monkeypatch.setattr(lps, "run_cpu", capture)
    monkeypatch.setattr(lps, "value_players", lambda rosters, info, today: ((), None))
    monkeypatch.setattr(lps, "dated_calendar", lambda: tuple(WEEKS))
    monkeypatch.setattr(lps, "games_left_today", games)
    asyncio.run(lps.league_projection(_league_info(), today=MON))
    current = next(p for p in seen["league"].schedule if p.matchup == 6)
    assert (current.home_score, current.away_score) == scores
    assert seen["league"].today_left == left
