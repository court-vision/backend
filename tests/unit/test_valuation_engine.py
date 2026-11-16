"""
League valuation: volume, noise, cohort, playoffs — each property on a pool
small enough to reason about.

Players are built from a per-game line and expected games; teams are split
across regular and playoff weeks by hand. The board wiring is
`test_draft_board_service.py`'s.
"""

from dataclasses import replace

import pytest

from services.scoring.category_rank import PoolRow
from services.scoring.models import CategoryDef, StatLine
from services.scoring.vocab import DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS
from services.valuation.engine import (
    DEFAULT_PLAYOFF_WEIGHT,
    LeagueModel,
    ProjectedPlayer,
    TeamWeeks,
    effective_games,
    full_games,
    games_share,
    playoff_weight_of,
    points_per_game,
    replacement_level,
    value_pool,
)

pytestmark = pytest.mark.unit

NINE_CAT = tuple(CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES)


def _row(id, **stats):
    base = dict(pts=12.0, reb=5.0, ast=3.0, stl=1.0, blk=0.5, tov=1.5,
                fgm=4.5, fga=10.0, fg3m=1.2, fg3a=3.5, ftm=2.0, fta=2.6, min=26.0)
    base.update(stats)
    line = StatLine(**base, gp=1.0)
    fpts = sum(w * line.get(k) for k, w in DEFAULT_POINT_WEIGHTS.items())
    return PoolRow(id=id, name=f"P{id}", team=None, gp=60, line=line, fpts_avg=fpts, fpts_total=fpts * 60)


def _p(id, games=70.0, team=None, **stats):
    return ProjectedPlayer(row=_row(id, **stats), games=games, team=team)


def _filler(start, n, **stats):
    """A pool of near-identical role players, varied a little so no category's spread is zero."""
    return [
        _p(start + i, pts=10.0 + (i % 7) * 0.8, reb=4.0 + (i % 5) * 0.6, ast=2.0 + (i % 4) * 0.5,
           stl=0.7 + (i % 3) * 0.15, blk=0.4 + (i % 4) * 0.12, tov=1.2 + (i % 3) * 0.2,
           fg3m=1.0 + (i % 5) * 0.2, **stats)
        for i in range(n)
    ]


def _cats(**kw):
    return LeagueModel(format="categories", categories=NINE_CAT, league_size=2, roster_size=10,
                       counted_weeks=23.0, **kw)


def _by_id(valued):
    return {v.row.id: v for v in valued}


class TestVolume:
    def test_more_games_ranks_higher_with_the_same_line(self):
        pool = _filler(100, 30) + [_p(1, games=75.0, pts=20.0), _p(2, games=50.0, pts=20.0)]
        v = _by_id(value_pool(pool, _cats()))
        assert v[1].z_sum > v[2].z_sum
        assert v[1].games > v[2].games

    def test_points_season_value_is_per_game_times_games(self):
        pool = [_p(1, games=70.0), _p(2, games=35.0)]
        v = _by_id(value_pool(pool, LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS)))
        assert v[1].value == v[2].value
        assert v[1].season_value == pytest.approx(2 * v[2].season_value, abs=0.2)

    def test_zero_games_contributes_nothing_above_average(self):
        pool = _filler(100, 30) + [_p(1, games=0.0, pts=35.0, reb=12.0, ast=9.0)]
        valued = value_pool(pool, _cats())
        assert valued[-1].row.id == 1 or _by_id(valued)[1].z_sum < 0


class TestNoise:
    def test_noise_shrinks_the_score_of_a_noisy_category(self):
        """Same spread in steals and rebounds; steals are noisier per unit, so they weigh less
        than plain z would — roto (no weekly matchups) keeps the plain z."""
        pool = _filler(100, 30) + [_p(1, stl=2.5)]
        h2h = _by_id(value_pool(pool, _cats()))[1].z["stl"]
        roto = _by_id(value_pool(pool, LeagueModel(format="roto", categories=NINE_CAT,
                                                   league_size=2, roster_size=10)))[1].z["stl"]
        assert 0 < h2h < roto

    def test_turnovers_are_signed_against(self):
        pool = _filler(100, 30) + [_p(1, tov=5.0)]
        assert _by_id(value_pool(pool, _cats()))[1].z["tov"] < 0


class TestCohort:
    def test_undraftable_tail_does_not_move_the_top(self):
        stars = [_p(1, pts=30.0, reb=10.0, ast=8.0), _p(2, pts=25.0, stl=2.0, blk=2.0)]
        base = stars + _filler(100, 18)
        scrubs = _filler(500, 200, min=8.0)
        for s in scrubs:
            object.__setattr__(s, "games", 20.0)
        a = _by_id(value_pool(base, _cats()))
        b = _by_id(value_pool(base + scrubs, _cats()))
        assert a[1].z_sum == pytest.approx(b[1].z_sum, abs=1e-6)
        assert a[2].z_sum == pytest.approx(b[2].z_sum, abs=1e-6)


class TestShooting:
    def test_volume_shooter_beats_low_volume_marksman(self):
        pool = _filler(100, 30) + [
            _p(1, fgm=11.0, fga=20.0),        # 55% on 20
            _p(2, fgm=1.2, fga=2.0),          # 60% on 2
        ]
        v = _by_id(value_pool(pool, _cats()))
        assert v[1].z["fg_pct"] > v[2].z["fg_pct"] > 0

    def test_no_attempts_is_neutral(self):
        pool = _filler(100, 30) + [_p(1, ftm=0.0, fta=0.0)]
        v = _by_id(value_pool(pool, _cats()))[1]
        assert v.cats["ft_pct"] is None
        assert v.z["ft_pct"] == pytest.approx(0.0, abs=0.2)


class TestPlayoffs:
    SPLITS = {"PHX": TeamWeeks(regular=62, playoff=16, after=4),
              "CLE": TeamWeeks(regular=65, playoff=13, after=4)}

    def test_lambda_one_counts_every_scored_game_once(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks=self.SPLITS, playoff_weight=1.0)
        phx = effective_games(_p(1, games=82.0, team="PHX"), m)
        cle = effective_games(_p(2, games=82.0, team="CLE"), m)
        assert phx == pytest.approx(78.0) and cle == pytest.approx(78.0)

    def test_weighting_playoffs_favours_the_heavy_playoff_schedule(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks=self.SPLITS, playoff_weight=3.0)
        v = _by_id(value_pool([_p(1, games=82.0, team="PHX"), _p(2, games=82.0, team="CLE")], m))
        assert v[1].season_value > v[2].season_value
        # renormalized: the league-average team's games are unchanged
        assert (v[1].games + v[2].games) / 2 == pytest.approx(78.0, abs=0.1)

    def test_games_after_the_playoffs_never_count(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks={"X": TeamWeeks(regular=60, playoff=12, after=10)}, playoff_weight=1.0)
        assert effective_games(_p(1, games=82.0, team="X"), m) == pytest.approx(72.0)

    def test_roto_ignores_the_playoff_weight(self):
        m = LeagueModel(format="roto", categories=NINE_CAT, team_weeks=self.SPLITS, playoff_weight=4.0)
        assert effective_games(_p(1, games=82.0, team="PHX"), m) == pytest.approx(82.0)

    def test_unknown_team_is_the_league_average(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks=self.SPLITS, playoff_weight=2.0)
        mid = effective_games(_p(1, games=82.0, team=None), m)
        lo = effective_games(_p(2, games=82.0, team="CLE"), m)
        hi = effective_games(_p(3, games=82.0, team="PHX"), m)
        assert lo < mid < hi

    def test_part_season_player_keeps_his_share(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks={"X": TeamWeeks(regular=64, playoff=14, after=4)}, playoff_weight=1.0)
        assert effective_games(_p(1, games=41.0, team="X"), m) == pytest.approx(39.0)

    def test_no_calendar_is_plain_expected_games(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS)
        assert effective_games(_p(1, games=61.0), m) == 61.0
        assert effective_games(ProjectedPlayer(row=_row(2)), m) == 65.0

    def test_the_share_is_his_games_over_his_teams_and_rides_on_the_valued_row(self):
        m = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS,
                        team_weeks={"X": TeamWeeks(regular=64, playoff=14, after=4)})
        assert games_share(_p(1, games=41.0, team="X"), m) == pytest.approx(0.5)
        assert games_share(_p(2, games=90.0, team="X"), m) == 1.0         # never more than every game
        # A team the calendar does not carry is the league-average one; no
        # calendar at all is an 82-game season; nobody projecting him is 65 games.
        assert games_share(_p(3, games=41.0, team="??"), m) == pytest.approx(0.5)
        bare = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS)
        assert games_share(_p(4, games=41.0), bare) == pytest.approx(0.5)
        assert games_share(ProjectedPlayer(row=_row(5)), bare) == pytest.approx(65 / 82)

        valued = _by_id(value_pool([_p(1, games=41.0, team="X"), _p(2, games=82.0, team="X")], m))
        assert valued[1].share == pytest.approx(0.5) and valued[2].share == 1.0
        cats = _by_id(value_pool([_p(1, games=41.0), _p(2, games=82.0)] + _filler(10, 20), _cats()))
        assert cats[1].share == pytest.approx(0.5) and cats[2].share == 1.0


class TestPoints:
    def test_double_double_weight_uses_the_projected_rate(self):
        weights = dict(DEFAULT_POINT_WEIGHTS, dd=5.0, td=10.0)
        p = ProjectedPlayer(row=_row(1), games=70.0, dd_rate=0.4, td_rate=0.1)
        q = ProjectedPlayer(row=_row(2), games=70.0)
        assert points_per_game(p, weights) - points_per_game(q, weights) == pytest.approx(3.0)

    def test_league_weights_change_the_order(self):
        big = _p(1, reb=13.0, blk=2.5, ast=1.0)
        guard = _p(2, reb=3.0, blk=0.2, ast=9.0)
        assists_heavy = dict(DEFAULT_POINT_WEIGHTS, ast=4.0)
        boards_heavy = dict(DEFAULT_POINT_WEIGHTS, reb=3.0)
        a = value_pool([big, guard], LeagueModel(format="points", point_weights=assists_heavy))
        b = value_pool([big, guard], LeagueModel(format="points", point_weights=boards_heavy))
        assert a[0].row.id == 2 and b[0].row.id == 1


class TestShape:
    def test_categories_keep_the_board_tuple_and_order(self):
        pool = _filler(100, 30) + [_p(1, pts=30.0, reb=10.0, ast=8.0)]
        valued = value_pool(pool, _cats())
        assert valued[0].row.id == 1
        sums = [v.z_sum for v in valued]
        assert sums == sorted(sums, reverse=True)
        top = valued[0]
        assert set(top.z) == set(DEFAULT_CATEGORIES)
        assert top.z_sum == pytest.approx(sum(top.z.values()), abs=0.01)
        assert top.value > 25.0

    def test_punted_categories_are_simply_absent(self):
        cats = tuple(c for c in NINE_CAT if c.key not in ("ft_pct", "tov"))
        pool = _filler(100, 30) + [_p(1)]
        v = _by_id(value_pool(pool, LeagueModel(format="categories", categories=cats,
                                                league_size=2, roster_size=10)))[1]
        assert "ft_pct" not in v.z and "tov" not in v.z

    def test_empty_pool(self):
        assert value_pool([], _cats()) == []


@pytest.mark.parametrize("raw,expected", [
    (None, DEFAULT_PLAYOFF_WEIGHT), ("abc", DEFAULT_PLAYOFF_WEIGHT), ("1", 1.0),
    (1.2, 1.0), (1.3, 1.5), (2.4, 2.0), (2.6, 3.0), (9, 4.0), (0, 1.0),
])
def test_playoff_weight_snaps_to_the_offered_steps(raw, expected):
    assert playoff_weight_of(raw) == expected


# ---- a missed game is played by a replacement (`fill_missed`) ---------------------------


ONE_TEAM = {"X": TeamWeeks(regular=40, playoff=10, after=0)}


def _fill_points(**kw):
    """A two-team, five-seat league: a cohort of 10, so the eleventh player per game is the replacement."""
    return LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS, league_size=2, roster_size=5,
                       team_weeks=ONE_TEAM, playoff_weight=1.0, fill_missed=True, **kw)


def _ladder(n=20, games=50.0):
    """Players 100.., each a point a game worse than the one before, healthy all the way."""
    return [_p(100 + i, games=games, team="X", pts=30.0 - i) for i in range(n)]


class TestReplacement:
    def test_the_level_is_the_first_player_past_the_cohort_who_is_expected_to_play(self):
        pool = _ladder()
        per_game = [points_per_game(p, DEFAULT_POINT_WEIGHTS) for p in pool]
        assert replacement_level(per_game, pool, 10) == per_game[10]
        # A player with no game left is nobody's replacement: the level moves down one
        gone = [_p(100, games=0.0, team="X", pts=30.0)] + pool[1:]
        assert replacement_level(per_game, gone, 10) == per_game[11]
        # A pool no deeper than the cohort has none
        assert replacement_level(per_game[:10], pool[:10], 10) == 0.0

    def test_a_missed_game_costs_the_gap_to_a_replacement(self):
        star = _p(1, games=30.0, team="X", pts=40.0)        # out for 20 of the 50 left
        pool = _ladder() + [star]
        model = _fill_points()
        v = _by_id(value_pool(pool, model))
        level = points_per_game(pool[9], DEFAULT_POINT_WEIGHTS)     # the star makes eleven above it
        fpg = points_per_game(star, DEFAULT_POINT_WEIGHTS)
        assert v[1].season_value == pytest.approx(fpg * 30 + level * 20, abs=0.1)
        # Without the rule the same player is 30 games of himself and 20 of nothing
        plain = _by_id(value_pool(pool, replace(model, fill_missed=False)))
        assert plain[1].season_value == pytest.approx(fpg * 30, abs=0.1)
        # His own games and his share of the schedule are still his own
        assert v[1].games == 30.0 and v[1].expected_games == 30.0 and v[1].share == pytest.approx(0.6)

    def test_a_star_out_for_weeks_stays_above_a_healthy_role_player(self):
        # 40 a game for 30 games against 24 a game for all 50: by totals 1200 plays 1200
        star = _p(1, games=30.0, team="X", pts=40.0)
        pool = _ladder() + [star]
        order = [v.row.id for v in value_pool(pool, _fill_points())]
        healthy_24 = 106
        assert order.index(1) < order.index(healthy_24)
        plain = [v.row.id for v in value_pool(pool, replace(_fill_points(), fill_missed=False))]
        assert plain.index(1) > plain.index(104)             # without the rule he sits under the 26-a-game player

    def test_a_player_below_the_replacement_gains_nothing_by_sitting(self):
        pool = _ladder()
        pool[15] = _p(115, games=10.0, team="X", pts=15.0)   # below the level, and out for 40
        v = _by_id(value_pool(pool, _fill_points()))
        fpg = points_per_game(pool[15], DEFAULT_POINT_WEIGHTS)
        assert v[115].season_value == pytest.approx(fpg * 50, abs=0.1)
        assert v[115].season_value < v[110].season_value     # never above the replacement himself

    def test_a_player_with_no_game_left_falls_off_the_board(self):
        pool = _ladder() + [_p(1, games=0.0, team="X", pts=40.0)]
        valued = value_pool(pool, _fill_points())
        assert valued[-1].row.id == 1 and valued[-1].season_value == 0.0

    def test_the_fill_counts_playoff_games_at_the_playoff_weight(self):
        # He misses the regular weeks and plays every playoff game: with the
        # playoffs doubled his own ten games count for twenty
        late = ProjectedPlayer(row=_row(1, pts=40.0), games=10.0, team="X", phase_games=(0.0, 10.0, 0.0))
        model = _fill_points()
        doubled = replace(model, playoff_weight=2.0)
        assert effective_games(late, model) == pytest.approx(10.0)
        norm = 50 / 60
        assert effective_games(late, doubled) == pytest.approx(20 * norm)
        assert full_games(late, doubled) == pytest.approx(50.0)
        # Spread evenly, the same ten games would be a fifth of each phase
        even = ProjectedPlayer(row=_row(2, pts=40.0), games=10.0, team="X")
        assert effective_games(even, doubled) == pytest.approx(0.2 * 60 * norm)

    def test_phase_games_in_roto_are_every_game_once(self):
        roto = LeagueModel(format="roto", categories=NINE_CAT, team_weeks={"X": TeamWeeks(40, 6, 4)},
                           playoff_weight=4.0)
        late = ProjectedPlayer(row=_row(1), games=20.0, team="X", phase_games=(12.0, 5.0, 3.0))
        assert effective_games(late, roto) == pytest.approx(20.0)
        assert full_games(late, roto) == 50.0

    def test_no_calendar_means_no_seat_to_fill(self):
        model = LeagueModel(format="points", point_weights=DEFAULT_POINT_WEIGHTS, league_size=2, roster_size=5,
                            fill_missed=True)
        pool = [_p(100 + i, games=50.0, pts=30.0 - i) for i in range(20)] + [_p(1, games=30.0, pts=40.0)]
        assert full_games(pool[-1], model) is None
        v = _by_id(value_pool(pool, model))
        assert v[1].season_value == pytest.approx(points_per_game(pool[-1], DEFAULT_POINT_WEIGHTS) * 30, abs=0.1)


def _fill_cats(**kw):
    return LeagueModel(format="categories", categories=NINE_CAT, league_size=2, roster_size=10,
                       counted_weeks=10.0, team_weeks=ONE_TEAM, playoff_weight=1.0, **kw)


class TestReplacementInCategories:
    def _pool(self, star_games):
        star = _p(1, games=star_games, team="X", pts=30.0, reb=10.0, ast=8.0, stl=1.8, blk=1.2, fg3m=3.0,
                  fgm=10.5, fga=20.0, ftm=6.0, fta=7.0)
        return _filler(100, 40, team="X", games=50.0) + [star]

    def test_an_injured_star_keeps_the_games_a_replacement_plays_for_him(self):
        pool = self._pool(star_games=25.0)
        filled = _by_id(value_pool(pool, _fill_cats(fill_missed=True)))
        plain = _by_id(value_pool(pool, _fill_cats()))
        healthy = _by_id(value_pool(self._pool(star_games=50.0), _fill_cats(fill_missed=True)))
        # Half his games at a replacement's line: worth more than half a season of
        # nothing, less than playing them himself
        assert plain[1].z_sum < filled[1].z_sum < healthy[1].z_sum
        # What he shows per game is still his own line, and his games his own
        assert filled[1].cats["pts"] == 30.0 and filled[1].games == 25.0

    def test_healthy_players_are_valued_as_before(self):
        # Nobody misses a game: there is nothing to fill, and the board is the old one
        pool = self._pool(star_games=50.0)
        filled = value_pool(pool, _fill_cats(fill_missed=True))
        plain = value_pool(pool, _fill_cats())
        assert [(v.row.id, v.z_sum) for v in filled] == [(v.row.id, v.z_sum) for v in plain]

    def test_a_player_with_no_game_left_is_last(self):
        pool = self._pool(star_games=0.0)
        valued = value_pool(pool, _fill_cats(fill_missed=True))
        assert valued[-1].row.id == 1

    def test_a_player_no_better_than_a_replacement_fills_his_own_seat(self):
        pool = self._pool(star_games=50.0)
        weak = _p(2, games=10.0, team="X", pts=4.0, reb=1.5, ast=0.8, stl=0.2, blk=0.1, fg3m=0.2,
                  fgm=1.5, fga=4.5, ftm=0.6, fta=1.0)
        strong = _by_id(value_pool(pool + [weak], _fill_cats(fill_missed=True)))
        # Sitting 40 of 50 does not lift him over the worst healthy player in the pool
        assert strong[2].z_sum == min(v.z_sum for v in strong.values())

    def test_roto_fills_the_season(self):
        pool = self._pool(star_games=25.0)
        roto = LeagueModel(format="roto", categories=NINE_CAT, league_size=2, roster_size=10,
                           team_weeks=ONE_TEAM)
        filled = _by_id(value_pool(pool, replace(roto, fill_missed=True)))
        plain = _by_id(value_pool(pool, roto))
        assert filled[1].z_sum > plain[1].z_sum
