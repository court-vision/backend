"""ESPN matchup periods map to weekly scoring-period ids; the day-granular status ids never leak in."""

from datetime import date

import pytest

from services.espn_service import espn_scoring_periods_for
from services.schedule_service import get_dates_for_scoring_periods, get_matchup_by_number


@pytest.mark.unit
def test_periods_come_from_the_map_or_fall_back_to_the_id():
    m = {"1": [1], "2": [2], "20": [20, 21], "21": [22, 23]}
    assert espn_scoring_periods_for(m, 2) == [2]
    assert espn_scoring_periods_for(m, 20) == [20, 21]
    assert espn_scoring_periods_for(m, 99) == [99]          # missing key → the id itself
    assert espn_scoring_periods_for({}, 4) == [4]


@pytest.mark.unit
def test_early_season_day_ids_do_not_extend_the_range():
    # Regression: day 8 of the season used to be appended to week 2's ids,
    # stretching the matchup to the end of "week 8".
    ids = espn_scoring_periods_for({"2": [2]}, 2)
    assert 8 not in ids
    start, end = get_dates_for_scoring_periods(ids)
    wk = get_matchup_by_number(2)
    assert (start, end) == (wk["start_date"], wk["end_date"])
    assert (end - start).days == 6


@pytest.mark.unit
def test_two_week_playoff_round_spans_both_weeks():
    start, end = get_dates_for_scoring_periods([22, 23])
    assert start == get_matchup_by_number(22)["start_date"]
    assert end == get_matchup_by_number(23)["end_date"]
    assert isinstance(start, date)
