"""
Integration: league rows round-trip through JSONB, teams link to leagues, and
PlayerValueService reproduces stored fpts under default weights while
recomputing under custom weights.

Runs against a disposable database (CI's cv_test service); the session fixture
applies backend/migrations from scratch and truncates between tests.
"""

from datetime import date, datetime

import pytest

from db.models.leagues import League
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User
from services.player_value_service import PlayerValueService
from services.scoring import DEFAULT_POINT_WEIGHTS, resolve_scoring_for_team


@pytest.mark.integration
def test_league_jsonb_round_trip_and_team_link(integration_db):
    user = User.create(email="it@courtvision.dev", clerk_user_id="user_it", created_at=datetime.utcnow())
    league = League.create(
        provider="espn", provider_league_id="123", season=2027, name="IT",
        scoring_type="categories", category_win_mode="each_category",
        categories=[{"key": "pts", "label": "PTS", "higher_is_better": True, "is_rate": False},
                    {"key": "tov", "label": "TO", "higher_is_better": False, "is_rate": False}],
        point_weights={}, matchup_periods={"periods": {"1": [1]}}, roster_slots={"PG": 1},
        raw_settings={"_sync": {"unsupported": [], "warnings": []}}, settings_synced_at=datetime.utcnow(),
    )
    team = Team.create(user_id=user.user_id, team_identifier="123T", league_info="{}", league=league)

    reloaded = League.get_by_id(league.id)
    assert reloaded.categories[1]["higher_is_better"] is False
    assert reloaded.matchup_periods["periods"]["1"] == [1]

    scoring = resolve_scoring_for_team(team.team_id)
    assert scoring.format == "categories" and scoring.categories.keys == ["pts", "tov"]
    assert resolve_scoring_for_team(Team.create(user_id=user.user_id, team_identifier="x", league_info="{}").team_id).format == "points"


@pytest.mark.integration
def test_player_values_match_stored_fpts_for_default_weights_and_recompute_otherwise(integration_db):
    player = Player.create(id=1001, name="Test Player", name_normalized="test player", espn_id=5001, team_id=None)
    PlayerRollingStats.create(
        player=player, as_of_date=date(2027, 1, 10), window_days=7, gp=3,
        fpts=40.0, pts=20.0, reb=10.0, ast=5.0, stl=2.0, blk=1.0, tov=3.0, min=32.0,
        fgm=8.0, fga=15.0, fg_pct=0.5333, fg3m=3.0, fg3a=6.0, fg3_pct=0.5, ftm=5.0, fta=6.0, ft_pct=0.8333,
    )
    default = PlayerValueService.rolling_avg_by_espn_id([5001], days=7)
    assert default[5001] == 40.0                                      # stored fpts fast path

    custom = PlayerValueService.rolling_avg_by_espn_id([5001], days=7, weights={"pts": 1, "reb": 1.2, "tov": -1})
    assert custom[5001] == pytest.approx(20 + 12 - 3, abs=0.05)

    line = PlayerValueService.rolling_lines_by_espn_id([5001], days=7)[5001]
    assert line.get("fg_pct") == pytest.approx(8 / 15, abs=1e-4) and line.gp == 3
    assert PlayerValueService.rolling_avg_by_espn_id([5001], days=7, weights=DEFAULT_POINT_WEIGHTS)[5001] == 40.0
