"""
Fantasy playoffs: which weeks a league's playoffs occupy, and each team's games
in them.

Settings are the shapes the ESPN and Yahoo parsers store in
`usr.leagues.matchup_periods`; calendars are fabricated per-day team sets so
every count below can be checked on paper. The last test reads the real
2026-27 calendar, because the ESPN default is only right if it lands on the
weeks a rolled league actually uses.
"""

import pytest

from services.draft_congestion import SampleWeek, week_from_calendar
from services.valuation.playoffs import (
    PlayoffWindow,
    default_window,
    playoff_schedule,
    playoff_window,
)

pytestmark = pytest.mark.unit

# The rolled 2026-27 league 426893737's scheduleSettings, as the ESPN parser stores it.
ESPN_4_TEAM_2_WEEK_ROUNDS = {
    "periods": {**{str(p): [p] for p in range(1, 20)}, "20": [20, 21], "21": [22, 23]},
    "period_count": 19,
    "period_length": 1,
    "playoff_period_length": 2,
    "playoff_team_count": 4,
}


class TestPlayoffWindow:
    def test_espn_league_reads_its_playoff_periods(self):
        w = playoff_window(ESPN_4_TEAM_2_WEEK_ROUNDS, "espn", 24)
        assert w.weeks == (20, 21, 22, 23)
        assert w.source == "league"
        assert w.rounds == 2
        assert w.round_weeks == ((20, 21), (22, 23))
        assert w.label == "weeks 20-23"

    def test_six_team_bracket_is_three_rounds(self):
        mp = {
            "periods": {**{str(p): [p] for p in range(1, 21)}, "21": [21], "22": [22], "23": [23]},
            "period_count": 20, "playoff_period_length": 1, "playoff_team_count": 6,
        }
        w = playoff_window(mp, "espn", 24)
        assert w.weeks == (21, 22, 23)
        assert w.rounds == 3

    def test_missing_playoff_periods_are_laid_end_to_end(self):
        mp = {
            "periods": {str(p): [p] for p in range(1, 21)},
            "period_count": 20, "playoff_period_length": 1, "playoff_team_count": 8,
        }
        w = playoff_window(mp, "espn", 24)
        assert w.weeks == (21, 22, 23)
        assert w.source == "league"

    def test_unsynced_league_gets_espn_default(self):
        assert playoff_window({}, "espn", 24) == default_window(24)
        assert playoff_window(None, None, 24).weeks == (20, 21, 22, 23)

    def test_one_team_playoff_is_no_bracket(self):
        mp = dict(ESPN_4_TEAM_2_WEEK_ROUNDS, playoff_team_count=1)
        assert playoff_window(mp, "espn", 24).source == "espn_default"

    def test_weeks_past_the_calendar_fall_back(self):
        mp = dict(ESPN_4_TEAM_2_WEEK_ROUNDS, period_count=23)
        w = playoff_window(mp, "espn", 24)
        assert w.source == "espn_default"

    def test_yahoo_start_week_through_end(self):
        w = playoff_window({"playoff_start_week": 21, "end_week": 23}, "yahoo", 24)
        assert w.weeks == (21, 22, 23)
        assert w.source == "yahoo_weeks_assumed"

    def test_yahoo_without_a_start_week_falls_back(self):
        assert playoff_window({"end_week": 23}, "yahoo", 24).source == "espn_default"

    def test_default_on_a_tiny_calendar_is_empty(self):
        assert default_window(3).weeks == ()
        assert default_window(3).label == "no playoff weeks"


def _week(number, *days):
    return SampleWeek(number=number, days=tuple(frozenset(d) for d in days))


class TestPlayoffSchedule:
    def test_counts_games_light_nights_and_back_to_backs(self):
        busy = {f"T{i}" for i in range(20)}             # 10 games: not light
        window = PlayoffWindow(weeks=(1, 2), source="league")
        weeks = [
            _week(1, {"PHX", "DAL"}, {"PHX"} | busy, set()),
            _week(2, {"PHX"}, {"DAL"}),
            _week(3, {"PHX", "DAL"}),                   # outside the window
        ]
        s = playoff_schedule(window, weeks)
        phx = s.teams["PHX"]
        assert phx.games == 3
        assert phx.per_week == (2, 1)
        assert phx.light == 2                           # the 10-game night is not light
        assert phx.back_to_backs == 1                   # day 0 -> day 1
        dal = s.teams["DAL"]
        assert dal.games == 2
        assert dal.back_to_backs == 0                   # day 1 of week 1 was busy but DAL was off
        assert s.games("PHX") == 3
        assert s.games(None) is None
        assert s.games("XXX") is None

    def test_back_to_back_across_a_week_boundary(self):
        window = PlayoffWindow(weeks=(1, 2), source="league")
        s = playoff_schedule(window, [_week(1, set(), {"MIA"}), _week(2, {"MIA"})])
        assert s.teams["MIA"].back_to_backs == 1

    def test_empty_window(self):
        s = playoff_schedule(PlayoffWindow(weeks=(), source="espn_default"), [_week(1, {"A"})])
        assert s.teams == {}
        assert s.mean_games == 0.0
        assert s.span == (0, 0)


def test_default_window_on_the_real_2026_27_calendar():
    """ESPN's default lands on weeks 20-23 and the spread is the one the plan quotes."""
    from services import schedule_service

    try:
        weeks = [week_from_calendar(w["matchup_number"], w["game_span"], w["games"])
                 for w in schedule_service.iter_weeks("2026-27")]
    except FileNotFoundError:
        pytest.skip("2026-27 calendar not on disk")
    window = playoff_window(None, None, len(weeks))
    assert window.weeks == (20, 21, 22, 23)
    s = playoff_schedule(window, weeks)
    assert len(s.teams) == 30
    assert s.span == (13, 16)
    assert s.games("PHX") == 16 and s.games("DAL") == 16
    assert s.games("LAL") == 13 and s.games("CLE") == 13
