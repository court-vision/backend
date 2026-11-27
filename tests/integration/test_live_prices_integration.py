"""The live prices over a real database and the real season calendar
(services/live_price_service.py): the morning's rows as data-platform's live_projection
pipeline writes them, read back and priced for the Week desk's decisions.

Today is pinned to Nov 16, 2026, inside the 2026-27 calendar.
"""

from datetime import date, timedelta

import pytest

from core.settings import settings
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from services import live_price_service as service
from services import live_projection
from services.scoring.resolver import ResolvedScoring

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 16)
STAR, HURT, FILL_IN = 930_001, 930_002, 930_003
POINTS = ResolvedScoring("points", None, True, {"pts": 1.0, "reb": 1.2})
RATES = {"pts": 0.6, "reb": 0.2, "ast": 0.1}


def _row(player_id, as_of, timeline, team="DEN", rates=RATES, source="cv_live"):
    return PlayerProjection.create(
        player=player_id, season="2026-27", source=source, as_of_date=as_of, projected_gp=60,
        min=30, pts=18, reb=6, ast=3, stl=1, blk=1, tov=2, fgm=7, fga=15, fg3m=2, fg3a=5, ftm=2, fta=3,
        raw={"version": "live-test", "games": 60.0, "team": team, "rates": rates, "timeline": timeline},
    )


@pytest.fixture
def morning(integration_db, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2026-27", raising=False)
    for pid, name in ((STAR, "Steady Star"), (HURT, "Hurt Today"), (FILL_IN, "Fill In")):
        Player.create(id=pid, espn_id=pid - 900_000, name=name, name_normalized=name.lower(), position="G")
    yesterday = TODAY - timedelta(days=1)
    _row(STAR, yesterday, [[yesterday.isoformat(), "2027-04-30", 75, 1.0, 34.0, 1.0]])
    _row(HURT, yesterday, [[yesterday.isoformat(), "2026-12-15", 14, 0.0, 30.0, 1.0],
                           ["2026-12-16", "2027-04-30", 60, 0.9, 30.0, 1.0]])
    # Starting while the starter is out, then back to the bench
    _row(FILL_IN, yesterday, [[yesterday.isoformat(), "2026-11-22", 3, 1.0, 32.0, 1.2],
                              ["2026-11-23", "2027-04-30", 70, 0.95, 14.0, 1.0]])
    # The plain morning beside it is not the one read
    _row(STAR, yesterday, [[yesterday.isoformat(), "2027-04-30", 75, 0.1, 10.0, 1.0]], source="cv_live_shadow")
    service.clear_cache()


def test_the_stored_rows_come_back_with_their_rates_and_each_runs_minutes_and_usage(morning):
    snapshot = live_projection.load("2026-27", TODAY)

    fill_in = snapshot.players[FILL_IN]
    assert fill_in.rates == RATES and fill_in.espn_id == FILL_IN - 900_000
    assert [(r.minutes, r.usage, r.chance) for r in fill_in.timeline] == [(32.0, 1.2, 1.0), (14.0, 1.0, 0.95)]
    tonight = live_projection.line_on(fill_in, TODAY)
    assert (tonight.pts, tonight.reb, tonight.min) == (pytest.approx(0.6 * 32 * 1.2), pytest.approx(0.2 * 32), 32.0)


def test_each_player_is_priced_on_the_real_calendar_from_today(morning):
    den = service._game_days(TODAY)["DEN"]
    assert den and den[0] >= TODAY and den[-1] > date(2027, 4, 1)

    found = service.prices(POINTS, [STAR], espn_ids=[HURT - 900_000, FILL_IN - 900_000, 1], today=TODAY)

    assert (found.as_of, found.today) == (TODAY - timedelta(days=1), TODAY)
    star, hurt, fill_in = found.get(STAR), found.of_espn(HURT - 900_000), found.of_espn(FILL_IN - 900_000)
    a_game = 0.6 * 34 + 1.2 * 0.2 * 34
    # Healthy: his line every game DEN has left, and tonight the same
    assert star.season.team_games == len(den) and star.season.per_game == pytest.approx(a_game, abs=0.01)
    assert star.on(den[0]) == pytest.approx(a_game, abs=0.01)
    # Out until mid-December: nothing this week, and less than his line over the season
    week = [d for d in den if d < TODAY + timedelta(days=7)]
    assert [hurt.on(d) for d in week] == [0.0] * len(week)
    assert 0 < hurt.season.per_game < 0.9 * (0.6 * 30 + 1.2 * 0.2 * 30)
    # Filling in: worth a pickup tonight, and a fraction of that over the season
    assert fill_in.on(week[0]) == pytest.approx(0.6 * 32 * 1.2 + 1.2 * 0.2 * 32, abs=0.01)
    assert fill_in.season.per_game < 0.5 * fill_in.on(week[0])
    assert found.season_by_espn_id() == {HURT - 900_000: hurt.season.per_game, FILL_IN - 900_000: fill_in.season.per_game}
    assert found.of_espn(1) is None


def test_a_morning_older_than_two_days_prices_nothing(morning):
    assert service.available(TODAY) and service.available(TODAY + timedelta(days=1))
    late = TODAY + timedelta(days=live_projection.FRESH_DAYS + 1)
    assert not service.available(late) and service.prices(POINTS, [STAR], today=late) is None
