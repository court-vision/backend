"""
A player priced over a window off the live projection (the Live rankings plan, step 8):
his line on a given night (`live_projection.run_line`), the price over his team's game
days (`live_prices.price`) and the read that hands prices to the Week desk's decisions
(`live_price_service.prices`).

The data platform writes the rows; these are the same shapes, built by hand.
"""

from datetime import date, timedelta

import pytest

from services import live_price_service as service
from services import live_projection as live
from services.live_prices import LivePrice, Window, price
from services.scoring.models import StatLine
from services.scoring.resolver import ResolvedScoring

pytestmark = pytest.mark.unit

MON = date(2026, 11, 16)
POINTS = ResolvedScoring("points", None, True, {"pts": 1.0, "reb": 1.0})
RATES = {"pts": 0.5, "reb": 0.2, "ast": 0.1, "min": 99.0, "unknown": 3.0}


def days(*offsets) -> list[date]:
    return [MON + timedelta(days=o) for o in offsets]


def score(line: StatLine) -> float:
    return POINTS.points.score(line)


def player(timeline, *, pid=1, team="DEN", rates=RATES, line=None, espn_id=9001) -> live.LivePlayer:
    return live.LivePlayer(
        player_id=pid, name=f"P{pid}", espn_id=espn_id, name_normalized=f"p{pid}", team=team,
        line=line or StatLine(pts=15.0, reb=6.0, min=30.0, gp=1.0), games=0.0,
        timeline=live.parse_timeline(timeline), rates=rates,
    )


# ---- his line on a night ----------------------------------------------------------------


def test_a_run_carries_its_minutes_and_usage_and_an_old_timeline_reads_without_them():
    full, short, odd = live.parse_timeline([
        ["2026-11-16", "2026-11-18", 2, 0.9, 33.1, 1.08],
        ["2026-11-20", "2026-11-22", 2, 0.5],                       # stored before either was read
        ["2026-11-24", "2026-11-26", 2, 0.5, "many", -1],
    ])
    assert (full.minutes, full.usage) == (33.1, 1.08)
    assert (short.minutes, short.usage) == (None, 1.0)
    assert (odd.minutes, odd.usage) == (None, 1.0)


def test_his_line_in_a_run_is_his_rates_over_its_minutes_with_the_ball_stats_scaled_by_usage():
    p = player([["2026-11-16", "2026-11-18", 2, 1.0, 30.0, 1.2]])
    line = live.run_line(p, p.timeline[0])
    assert line.pts == pytest.approx(0.5 * 30.0 * 1.2)             # follows his share of the offense
    assert line.ast == pytest.approx(0.1 * 30.0 * 1.2)
    assert line.reb == pytest.approx(0.2 * 30.0)                   # follows his minutes alone
    assert (line.min, line.gp) == (30.0, 1.0)                      # the run's minutes, never a stored rate


def test_without_rates_or_minutes_his_season_line_stands_in():
    no_rates = player([["2026-11-16", "2026-11-18", 2, 1.0, 30.0, 1.2]], rates={})
    no_minutes = player([["2026-11-16", "2026-11-18", 2, 1.0]])
    assert live.run_line(no_rates, no_rates.timeline[0]) is no_rates.line
    assert live.run_line(no_minutes, no_minutes.timeline[0]) is no_minutes.line
    assert live.run_line(no_rates, None) is no_rates.line


def test_a_day_takes_the_run_it_falls_in_and_the_nearest_earlier_one_when_none_covers_it():
    p = player([["2026-11-16", "2026-11-18", 2, 1.0, 36.0, 1.0], ["2026-11-22", "2026-11-30", 4, 1.0, 24.0, 1.0]])
    assert live.line_on(p, MON).min == 36.0
    assert live.line_on(p, MON + timedelta(days=4)).min == 36.0    # between the runs: the earlier one
    assert live.line_on(p, MON + timedelta(days=6)).min == 24.0
    assert live.line_on(p, MON - timedelta(days=3)).min == 36.0    # before any: the first
    assert live.run_on((), MON) is None and live.chance_on((), MON) == 0.0


# ---- the price ---------------------------------------------------------------------------


def test_a_healthy_player_is_worth_his_line_every_game():
    p = player([["2026-11-16", "2026-12-31", 20, 1.0, 30.0, 1.0]])
    found = price(p, score, days(0, 2, 4, 7, 9), reach=MON + timedelta(days=6))
    per_game = 0.5 * 30 + 0.2 * 30
    assert found.season == Window(round(5 * per_game, 2), 5.0, 5)
    assert found.season.per_game == per_game
    assert found.days == {d: per_game for d in days(0, 2, 4)}      # day by day through the reach only
    assert found.on(MON) == per_game and found.on(MON + timedelta(days=1)) is None
    assert found.over(MON, MON + timedelta(days=2)) == 2 * per_game


def test_a_player_out_for_two_weeks_is_worth_nothing_until_he_is_back_and_less_over_the_season():
    out = player([["2026-11-16", "2026-11-29", 6, 0.0, 30.0, 1.0], ["2026-11-30", "2026-12-31", 14, 0.9, 30.0, 1.0]])
    found = price(out, score, days(0, 2, 4, 14, 16), reach=MON + timedelta(days=20))
    assert [found.on(d) for d in days(0, 2, 4)] == [0.0, 0.0, 0.0]
    assert found.on(MON + timedelta(days=14)) == pytest.approx(0.9 * 21.0)
    assert found.season.games == pytest.approx(1.8) and found.season.team_games == 5
    # per game on his team's schedule: the games he misses count against him
    assert found.season.per_game == pytest.approx(0.9 * 21.0 * 2 / 5, abs=0.01)


def test_a_role_that_ends_when_the_starter_returns_counts_only_for_the_games_it_lasts():
    filling_in = player([["2026-11-16", "2026-11-18", 2, 1.0, 34.0, 1.15], ["2026-11-20", "2026-12-31", 18, 1.0, 20.0, 1.0]])
    found = price(filling_in, score, days(0, 2, 4, 7, 9, 11), reach=MON + timedelta(days=6))
    tonight = 0.5 * 34 * 1.15 + 0.2 * 34
    later = 0.5 * 20 + 0.2 * 20
    assert found.on(MON) == pytest.approx(tonight, abs=0.01) and found.on(MON + timedelta(days=4)) == later
    assert found.season.per_game == pytest.approx((2 * tonight + 4 * later) / 6, abs=0.01)
    assert found.on(MON) > 1.4 * found.season.per_game             # tonight's stream, not a season's hold


def test_a_line_that_scores_below_zero_is_worth_nothing_not_a_debt():
    p = player([["2026-11-16", "2026-12-31", 20, 1.0, 30.0, 1.0]])
    found = price(p, lambda line: -4.0, days(0, 2), reach=MON)
    assert found.season.value == 0.0 and found.on(MON) == 0.0


def test_nothing_to_price_on_is_no_price():
    assert price(player([]), score, days(0, 2), reach=MON) is None
    assert price(player([["2026-11-16", "2026-12-31", 20, 1.0, 30.0, 1.0]]), score, [], reach=MON) is None


@pytest.mark.parametrize("seed", range(25))
def test_the_price_is_the_sum_of_every_game_at_its_own_run(seed):
    """Against the slow way: each game day asked its run, one at a time."""
    import random

    rng = random.Random(seed)
    starts = sorted(rng.sample(range(-5, 60), rng.randint(1, 5)))
    timeline = [[(MON + timedelta(days=s)).isoformat(), (MON + timedelta(days=s + rng.randint(0, 9))).isoformat(),
                 rng.randint(1, 5), round(rng.random(), 3), round(rng.uniform(8, 38), 1), round(rng.uniform(0.8, 1.3), 3)]
                for s in starts]
    p = player(timeline)
    game_days = days(*sorted(rng.sample(range(0, 70), rng.randint(1, 30))))
    reach = MON + timedelta(days=21)

    found = price(p, score, game_days, reach)

    each = {d: live.chance_on(p.timeline, d) * score(live.line_on(p, d)) for d in game_days}
    assert found.season.value == pytest.approx(sum(each.values()), abs=0.01)
    assert found.season.games == pytest.approx(sum(live.chance_on(p.timeline, d) for d in game_days), abs=0.01)
    assert found.season.team_games == len(game_days)
    assert found.days == {d: pytest.approx(v, abs=0.006) for d, v in each.items() if d <= reach}


# ---- the read ----------------------------------------------------------------------------


HEALTHY = [["2026-11-16", "2026-12-31", 20, 1.0, 30.0, 1.0]]


def test_prices_come_by_nba_id_or_espn_id_for_the_players_the_projection_carries(live_prices):
    live_prices([player(HEALTHY, pid=1, espn_id=9001), player(HEALTHY, pid=2, espn_id=9002, team="BOS"),
                 player(HEALTHY, pid=3, espn_id=None, team=None)],
                {"DEN": days(0, 2, 4), "BOS": days(-1, 1)}, as_of=MON)

    found = service.prices(POINTS, [1, 3, 77, None], espn_ids=[9002, 555, None], today=MON)

    assert (found.as_of, found.today) == (MON, MON)
    assert set(found.by_id) == {1, 2}                              # 3 has no team, 77 is not carried
    assert found.get(1).season.team_games == 3 and found.get(None) is None
    assert found.of_espn(9002).season.team_games == 1              # a game before today is not priced
    assert found.of_espn(555) is None and found.of_espn(9001) is None   # 9001 was asked for by NBA id
    assert found.season_by_espn_id() == {9002: 21.0}


def test_no_prices_for_a_category_league_or_one_that_scores_double_doubles(live_prices):
    live_prices([player(HEALTHY)], {"DEN": days(0)}, as_of=MON)
    categories = ResolvedScoring("categories", None, True, {"pts": 1.0})
    double_doubles = ResolvedScoring("points", None, True, {"pts": 1.0, "dd": 3.0})

    assert service.prices(categories, [1], today=MON) is None
    assert service.prices(double_doubles, [1], today=MON) is None
    assert service.prices(POINTS, [1], today=MON) is not None


def test_no_fresh_projection_is_no_prices_and_a_failed_read_is_too(monkeypatch):
    assert service.prices(POINTS, [1], today=MON) is None          # the suite's default: nothing stored

    def down(today):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "_load", down)
    service.clear_cache()
    assert service.prices_soft(POINTS, [1], today=MON) is None
    with pytest.raises(RuntimeError):
        service.prices(POINTS, [1], today=MON)


def test_the_days_inputs_are_read_once_and_kept(monkeypatch):
    calls = []

    def load(today):
        calls.append(today)
        return service._Inputs(live.LiveSnapshot(as_of=MON, version="v", players={1: player(HEALTHY)}),
                               {"DEN": tuple(days(0, 2))}, {9001: 1})

    monkeypatch.setattr(service, "_load", load)
    service.clear_cache()
    for _ in range(3):
        assert service.prices(POINTS, espn_ids=[9001], today=MON).of_espn(9001) is not None
    assert calls == [MON]
    service.prices(POINTS, [1], today=MON + timedelta(days=1))     # another fantasy day reads again
    assert calls == [MON, MON + timedelta(days=1)]


def test_each_teams_game_days_come_off_the_season_calendar_from_today_on(monkeypatch):
    weeks = [
        {"start_date": MON, "game_span": 7, "games": {"DEN": {"0": True, "2": True, "9": True}, "BOS": {"1": True}}},
        {"start_date": MON + timedelta(days=7), "game_span": 7, "games": {"DEN": {"0": True}, "BOS": {}}},
    ]
    monkeypatch.setattr(service.schedule_service, "iter_weeks", lambda: iter(weeks))

    found = service._game_days(MON + timedelta(days=1))

    assert found == {"DEN": tuple(days(2, 7)), "BOS": tuple(days(1))}   # day 9 is past the week's span


def test_a_price_reads_as_nothing_special_when_empty():
    assert LivePrice().on(MON) is None and LivePrice().season.per_game is None
