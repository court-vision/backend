"""
The draft board in season (services/draft_in_season.py): which rooms value
in season, the blend of this season's games, the calendar from today, the
games left, and today's injury flags in the board's vocabulary.
"""

from datetime import date, datetime

import pytest

from services import draft_in_season as ins
from services.draft_congestion import SampleWeek
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine

pytestmark = pytest.mark.unit

OPENING = date(2026, 10, 20)
END = date(2027, 4, 11)
TODAY = date(2026, 11, 15)


def _row(pid, fpts, gp=1, team="DEN", **stats) -> PoolRow:
    return PoolRow(id=pid, name=f"P{pid}", team=team, gp=gp, line=StatLine.from_dict({**stats, "gp": 1}),
                   fpts_avg=fpts, fpts_total=round(fpts * gp, 1))


# ---- which rooms ----

def test_a_draft_in_season_counts_from_today():
    assert ins.games_from(TODAY, OPENING, END) == TODAY
    assert ins.games_from(TODAY, OPENING, END, kind="live", started=TODAY) == TODAY
    assert ins.games_from(OPENING, OPENING, END, kind="mock", started=OPENING) == OPENING


def test_a_draft_before_opening_night_keeps_the_preseason_valuation():
    assert ins.games_from(date(2026, 10, 1), OPENING, END) is None
    # A room that drafted in October keeps October's values when it is read now
    assert ins.games_from(TODAY, OPENING, END, kind="live", started=date(2026, 10, 12)) is None
    # An import records a draft that already happened, whenever it is imported
    assert ins.games_from(TODAY, OPENING, END, kind="import", started=TODAY) is None
    # After the regular season, and with no calendar
    assert ins.games_from(date(2027, 5, 1), OPENING, END) is None
    assert ins.games_from(TODAY, None, None) is None


def test_the_day_a_room_drafts():
    made = datetime(2026, 10, 1, 12)
    # The first pick says when
    assert ins.drafted_on(datetime(2026, 11, 14, 23), made, "active", TODAY) == date(2026, 11, 14)
    # A room made in October still waiting for its first pick drafts now
    assert ins.drafted_on(None, made, "active", TODAY) == TODAY
    # A finished room that never recorded one drafted when it was made
    assert ins.drafted_on(None, made, "completed", TODAY) == date(2026, 10, 1)
    assert ins.drafted_on(None, None, "completed", TODAY) is None


# ---- the blend ----

def test_this_season_counts_by_games_played_against_the_priors_ten():
    prior = _row(1, fpts=40.0, gp=70, pts=20, fgm=8, fga=16)
    # Ten games: half this season, half the prior
    current = _row(1, fpts=50.0, gp=10, team="LAL", pts=30, fgm=12, fga=20)

    blended = ins.blend_line(prior, current)

    assert blended.fpts_avg == 45.0
    assert blended.line.pts == pytest.approx(25.0)
    # Shooting stays makes over attempts of the blended line
    assert blended.line.get("fg_pct") == pytest.approx(10 / 18)
    assert blended.team == "LAL" and blended.gp == 70
    # Five games count a third
    assert ins.blend_line(prior, _row(1, fpts=55.0, gp=5)).fpts_avg == 45.0
    # No games: the prior as it is
    assert ins.blend_line(prior, _row(1, fpts=99.0, gp=0)) is prior


def test_apply_blends_the_pool_and_values_a_newcomer_only_on_enough_games():
    pool = {1: _row(1, fpts=40.0, gp=70), 2: _row(2, fpts=30.0, gp=60)}
    source = {1: "projection", 2: "baseline"}
    current = [
        _row(1, fpts=50.0, gp=10),
        _row(7, fpts=35.0, gp=12),     # nothing projects him, and he has the games
        _row(8, fpts=60.0, gp=3),      # three hot games are not a value
        _row(9, fpts=20.0, gp=0),
    ]

    found = ins.apply(pool, source, current, TODAY, season_as_of=date(2026, 11, 14))

    assert pool[1].fpts_avg == 45.0 and pool[2].fpts_avg == 30.0
    assert source == {1: "projection", 2: "baseline", 7: "season"} and 8 not in pool
    assert pool[7].fpts_avg == 35.0
    assert found.season_gp == {1: 10, 7: 12, 8: 3} and found.season_fpts[8] == 60.0
    assert (found.blended, found.season_only, found.games_from) == (1, 1, TODAY)


# ---- the calendar and the games left ----

def test_the_calendar_keeps_only_the_days_still_to_come():
    weeks = (
        ins.DatedWeek(date(2026, 11, 2), SampleWeek(3, (frozenset({"DEN"}),) * 7)),
        ins.DatedWeek(date(2026, 11, 9), SampleWeek(4, tuple(frozenset({"DEN", f"T{i}"}) for i in range(7)))),
        ins.DatedWeek(date(2026, 11, 16), SampleWeek(5, (frozenset({"BOS"}),) * 7)),
    )

    trimmed = ins.trim_calendar(weeks, TODAY)

    # A week already played keeps its number with nothing in it
    assert [w.number for w in trimmed] == [3, 4, 5]
    assert trimmed[0].days == ()
    # Nov 15 is the last day of week 4
    assert trimmed[1].days == (frozenset({"DEN", "T6"}),)
    assert len(trimmed[2].days) == 7
    assert ins.trim_calendar(weeks, None) == tuple(w.week for w in weeks)
    assert ins.team_games(trimmed) == {"DEN": 1, "T6": 1, "BOS": 7}


def test_the_games_left_are_the_projections_share_of_what_his_team_has_left():
    full, left = {"DEN": 82, "BOS": 80}, {"DEN": 41, "BOS": 60}
    assert ins.games_left(70, "DEN", full, left) == 35.0
    # Nothing projects him: a typical season's share
    assert ins.games_left(None, "BOS", full, left) == pytest.approx(65 * 60 / 80)
    # A team the calendar doesn't know: the league's share
    assert ins.games_left(70, None, full, left) == pytest.approx(70 * 101 / 162, abs=0.01)
    assert ins.games_left(70, "DEN", {}, {}) == 70.0


# ---- today's injuries ----

def test_live_statuses_in_the_boards_vocabulary():
    assert ins.live_status("Out") == "OUT"
    assert ins.live_status("Doubtful") == "DOUBTFUL"
    assert ins.live_status("Questionable") == "QUESTIONABLE"
    assert ins.live_status("Day-To-Day") == "DAY_TO_DAY"
    # Cleared to play: no flag
    assert ins.live_status("Probable") is None and ins.live_status("Available") is None
    assert ins.live_status(None) is None
    assert ins.live_status("Suspended") == "SUSPENDED"
