"""
Integration: what stands behind a player's board value, and the season
projection as a prior on the board's own scale.

The daily swap (services.daily_actions_service.drop_value) weighs a drop
candidate at his recent average only when that average rests on enough games,
and at his season projection otherwise. These are the two lookups it reads:
games in the rolling window, and the projection valued under the league's
scoring. The projection's source rule (CV's snapshot once published, ESPN's
until then, never a mix) and the category path (the projected line z-scored in
the player's place in the rolling pool) both live in queries a fake cannot check.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from core.settings import settings
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.players import Player
from services.player_value_service import PlayerValueService
from services.scoring.category_value import category_values, rankable_categories
from services.scoring.models import StatLine
from services.scoring.resolver import ResolvedScoring, resolve_scoring

SEASON = settings.nba_season
TODAY = date.today()
STAR = {"pts": 28, "reb": 5, "ast": 9, "stl": 1.5, "blk": 0.5, "tov": 3, "fgm": 10, "fga": 21,
        "fg3m": 3, "fg3a": 8, "ftm": 5, "fta": 6, "min": 34}


def _player(player_id: int, name: str | None = None) -> Player:
    return Player.create(id=player_id, name=name or f"P{player_id}", name_normalized=(name or f"p{player_id}").lower(),
                         espn_id=9000 + player_id)


def _rolling(player_id: int, gp: int, pts: float, *, days: int = 14, as_of: date = TODAY) -> None:
    PlayerRollingStats.create(
        player=player_id, as_of_date=as_of, window_days=days, gp=gp, fpts=pts * 1.5,
        pts=pts, reb=pts / 4, ast=pts / 6, stl=1.0, blk=0.5, tov=2.0, min=30.0,
        fgm=pts / 2.5, fga=pts / 1.2, fg_pct=0.48, fg3m=1.0, fg3a=3.0, fg3_pct=0.333, ftm=2.0, fta=2.5, ft_pct=0.8,
    )


@pytest.mark.integration
def test_games_behind_the_value_come_from_the_latest_fresh_window(integration_db):
    _player(1), _player(2), _player(3)
    _rolling(1, gp=6, pts=20)
    _rolling(2, gp=1, pts=8)
    _rolling(1, gp=4, pts=20, as_of=TODAY - timedelta(days=1))     # an older snapshot never counts
    _rolling(3, gp=7, pts=20, days=7)                               # nor another window

    assert PlayerValueService.rolling_games_by_espn_id([9001, 9002, 9003, 9999]) == {9001: 6, 9002: 1}
    assert PlayerValueService.rolling_games_by_espn_id([]) == {}
    with pytest.raises(ValueError):
        PlayerValueService.rolling_games_by_espn_id([9001], days=10)


@pytest.mark.integration
def test_a_stale_window_has_no_games_behind_anyone(integration_db):
    _player(1)
    _rolling(1, gp=6, pts=20, as_of=TODAY - timedelta(days=120))    # last season's L14 read in October
    assert PlayerValueService.rolling_games_by_espn_id([9001]) == {}


@pytest.mark.integration
def test_the_projection_is_cvs_latest_snapshot_and_never_a_mix(integration_db):
    _player(1, "Tyrese Haliburton"), _player(2, "Projected By ESPN Only")
    PlayerProjection.record_projection(1, SEASON, TODAY - timedelta(days=1), {"pts": 15}, source="cv")
    PlayerProjection.record_projection(1, SEASON, TODAY, STAR, projected_gp=62, source="cv")
    PlayerProjection.record_projection(1, SEASON, TODAY, {"pts": 10}, source="espn")
    PlayerProjection.record_projection(2, SEASON, TODAY, {"pts": 12}, source="espn")

    rows = PlayerValueService.projections_by_espn_id([9001, 9002])
    assert set(rows) == {9001} and float(rows[9001].pts) == 28.0     # CV's newest; ESPN's line is not mixed in

    PlayerProjection.delete().where(PlayerProjection.source == "cv").execute()
    rows = PlayerValueService.projections_by_espn_id([9001, 9002])
    assert {eid: float(r.pts) for eid, r in rows.items()} == {9001: 10.0, 9002: 12.0}   # ESPN's until CV publishes
    assert PlayerValueService.projections_by_espn_id([9001], season="1999-00") == {}


@pytest.mark.integration
def test_a_points_league_values_the_projection_under_its_own_weights(integration_db):
    _player(1, "Tyrese Haliburton")
    PlayerProjection.record_projection(1, SEASON, TODAY, STAR, projected_gp=62, source="cv")

    default = resolve_scoring(None)
    expected = round(default.points.score(StatLine.from_dict(STAR)), 1)
    assert PlayerValueService.projected_value_by_espn_id(default, [9001, 9002]) == {9001: expected}
    assert expected > 30

    weights = ResolvedScoring("points", None, True, {"pts": 1.0, "ast": 2.0, "tov": -1.0})
    assert PlayerValueService.projected_value_by_espn_id(weights, [9001]) == {9001: 28 + 18 - 3}


@pytest.mark.integration
def test_a_category_league_scores_the_projection_in_the_players_place_in_the_rolling_pool(integration_db):
    for pid in range(1, 9):
        _player(pid)
        _rolling(pid, gp=1 if pid == 1 else 6, pts=6 + 3 * pid)     # player 1: one quiet game so far...
    PlayerProjection.record_projection(1, SEASON, TODAY, STAR, projected_gp=62, source="cv")   # ...projected a star

    nine_cat = resolve_scoring(None, "categories")
    projected = PlayerValueService.projected_value_by_espn_id(nine_cat, [9001])

    pool = {row.id: row for row in PlayerValueService._category_pool(14)[1]}
    pool[1] = replace(pool[1], line=StatLine.from_dict(STAR), gp=62)
    expected = category_values(list(pool.values()), rankable_categories(nine_cat))[1][0]
    assert projected == {9001: expected}
    on_the_board = PlayerValueService.category_value_by_espn_id([9001], rankable_categories(nine_cat))[9001].value
    assert expected > on_the_board                                  # the prior lifts him off the bottom
