"""Daily/weekly matchup building blocks under points and category scoring."""

from datetime import date
from types import SimpleNamespace

import pytest

from schemas.matchup import MatchupData, MatchupPlayerResp, MatchupTeamResp
from services.matchup_days import build_day, build_past_roster, index_games, score_stat_row, team_day_totals
from services.scoring import CategoryDef, CategoryScoring, DEFAULT_CATEGORIES, DEFAULT_POINT_WEIGHTS
from services.scoring.resolver import ResolvedScoring


def _row(**kw):
    base = dict(fpts=30, pts=20, reb=5, ast=5, stl=1, blk=1, tov=2, min=30, fgm=8, fga=16, fg3m=2, fg3a=5, ftm=2, fta=2)
    base.update(kw)
    return SimpleNamespace(**base)


def _player(pid, name, team, slot="PG"):
    return MatchupPlayerResp(player_id=pid, name=name, team=team, position="PG", lineup_slot=slot,
                             avg_points=20.0, projected_points=0.0, games_remaining=1, injured=False)


def _matchup():
    return MatchupData(
        matchup_period=3, matchup_period_start="2027-01-11", matchup_period_end="2027-01-17",
        your_team=MatchupTeamResp(team_name="You", team_id=1, current_score=0, projected_score=0,
                                  roster=[_player(1, "A One", "BOS"), _player(2, "B Two", "LAL")]),
        opponent_team=MatchupTeamResp(team_name="Opp", team_id=2, current_score=0, projected_score=0,
                                      roster=[_player(3, "C Three", "BOS")]),
        projected_winner="You", projected_margin=0,
    )


POINTS = ResolvedScoring("points", None, False, dict(DEFAULT_POINT_WEIGHTS))
CUSTOM = ResolvedScoring("points", None, True, {"pts": 2.0, "reb": 1.0})
CATS = ResolvedScoring("categories", None, True, dict(DEFAULT_POINT_WEIGHTS),
                       CategoryScoring([CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES]))
GAME = SimpleNamespace(home_team_id="BOS", away_team_id="LAL", start_time_et="19:30")


@pytest.mark.unit
def test_score_stat_row_uses_stored_fpts_for_default_and_weights_otherwise():
    assert score_stat_row(_row(), POINTS) == 30.0
    assert score_stat_row(_row(), CUSTOM) == 45.0


@pytest.mark.unit
def test_past_roster_orders_players_with_stats_first():
    resolve = {1: 101, 2: 102, 3: 103}.get
    teams_playing, _ = index_games([GAME])
    roster = build_past_roster([_player(1, "A", "BOS"), _player(2, "B", "LAL")], lambda p: resolve(p.player_id),
                               teams_playing, {102: _row(fpts=40, pts=30)}, POINTS)
    assert [p.player_id for p in roster] == [2, 1]
    assert roster[0].fpts == 40.0 and roster[0].pts == 30 and roster[1].fpts is None and roster[1].had_game


@pytest.mark.unit
def test_team_day_totals_categories_use_summed_makes_attempts():
    resolve = {1: 101, 2: 102}.get
    roster = build_past_roster([_player(1, "A", "BOS"), _player(2, "B", "LAL")], lambda p: resolve(p.player_id),
                               {"BOS", "LAL"}, {101: _row(fgm=5, fga=10), 102: _row(fgm=9, fga=10)}, CATS)
    total, cats = team_day_totals(roster, CATS)
    assert total == 60.0
    assert cats["fg_pct"] == pytest.approx(14 / 20, abs=1e-4) and cats["pts"] == 40
    assert team_day_totals(roster, POINTS)[1] is None


@pytest.mark.unit
def test_build_day_past_and_future_shapes():
    md = _matchup()
    resolve = {1: 101, 2: 102, 3: 103}.get
    stats = {101: _row(), 102: _row(tov=5), 103: _row(pts=10)}
    day = build_day(md, date(2027, 1, 12), date(2027, 1, 14), date(2027, 1, 11), stats, [GAME],
                    lambda p: resolve(p.player_id), CATS)
    assert day.day_type == "past" and day.day_index == 1 and day.scoring_format == "categories"
    assert day.your_team.total_fpts == 60.0 and day.your_team.categories["tov"] == 7
    assert day.category_comparison.wins >= 1
    winners = {i.key: i.winner for i in day.category_comparison.items}
    assert winners["tov"] == "opp"          # you: 7 turnovers vs opp: 2 -> opponent wins TO

    future = build_day(md, date(2027, 1, 16), date(2027, 1, 14), date(2027, 1, 11), {}, [GAME],
                       lambda p: resolve(p.player_id), CATS)
    assert future.day_type == "future" and future.your_team.total_fpts is None
    assert future.category_comparison is None
    assert future.your_team.roster[0].has_game and future.your_team.roster[0].opponent in ("vs LAL", "@ BOS")

    points_day = build_day(md, date(2027, 1, 12), date(2027, 1, 14), date(2027, 1, 11), stats, [GAME],
                           lambda p: resolve(p.player_id), POINTS)
    assert points_day.scoring_format == "points" and points_day.your_team.categories is None
    assert set(points_day.model_dump().keys()) == set(day.model_dump().keys())   # daily/weekly share one shape
