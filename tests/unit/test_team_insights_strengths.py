"""
Team insights category strengths: volume-weighted team totals over the L14 window
and the best-effort opponent comparison.
"""

import asyncio
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus, FantasyProvider
from schemas.espn import PlayerResp
from services.scoring.models import StatLine
from services.scoring.resolver import resolve_scoring
from services.team_insights_service import (
    STRENGTHS_WINDOW_DAYS,
    _opponent_comparison,
    _roster_lines,
    _strengths_from_line,
)


def _player(player_id: int, name: str, team: str = "DEN") -> PlayerResp:
    return PlayerResp(player_id=player_id, name=name, avg_points=0.0, team=team,
                      valid_positions=["PG"], injured=False)


@pytest.mark.unit
def test_percentages_are_volume_weighted_not_a_mean_of_player_pcts():
    total = StatLine.sum([StatLine(fgm=10, fga=20, ftm=4, fta=5), StatLine(fgm=1, fga=1, ftm=1, fta=1)])
    strengths = _strengths_from_line(total)

    assert strengths.avg_fg_pct == pytest.approx(11 / 21, abs=1e-4)     # not (0.5 + 1.0) / 2 = 0.75
    assert strengths.avg_ft_pct == pytest.approx(5 / 6, abs=1e-4)
    assert strengths.avg_fga == 21.0 and strengths.avg_fta == 6.0


@pytest.mark.unit
def test_strengths_are_team_per_game_totals_with_fractional_pcts():
    lines = [
        StatLine(pts=25.5, reb=8.0, ast=6.0, stl=1.5, blk=0.5, tov=3.0, fgm=9.0, fga=18.0, fg3m=2.5, ftm=5.0, fta=6.0),
        StatLine(pts=12.0, reb=10.0, ast=2.0, stl=0.5, blk=2.0, tov=1.0, fgm=5.0, fga=9.0, fg3m=0.0, ftm=2.0, fta=4.0),
    ]
    strengths = _strengths_from_line(StatLine.sum(lines))

    assert strengths.avg_points == 37.5 and strengths.avg_rebounds == 18.0 and strengths.avg_assists == 8.0
    assert strengths.avg_steals == 2.0 and strengths.avg_blocks == 2.5 and strengths.avg_turnovers == 4.0
    assert strengths.avg_fg3m == 2.5 and strengths.avg_fga == 27.0 and strengths.avg_fta == 10.0
    assert 0.0 <= strengths.avg_fg_pct <= 1.0 and 0.0 <= strengths.avg_ft_pct <= 1.0
    assert strengths.avg_fg_pct == pytest.approx(14 / 27, abs=1e-4)
    assert strengths.avg_ft_pct == pytest.approx(0.7, abs=1e-4)
    assert strengths.window_days == STRENGTHS_WINDOW_DAYS == 14


@pytest.mark.unit
def test_no_attempts_gives_zero_pct_not_an_error():
    strengths = _strengths_from_line(StatLine(pts=10))
    assert strengths.avg_fg_pct == 0.0 and strengths.avg_ft_pct == 0.0


@pytest.mark.unit
def test_roster_lines_espn_uses_l14_and_skips_players_without_data(monkeypatch):
    from services import team_insights_service

    captured = {}

    def fake_by_espn_id(espn_ids, days=7):
        captured["ids"], captured["days"] = list(espn_ids), days
        return {1: StatLine(pts=20), 2: None}

    monkeypatch.setattr(team_insights_service.PlayerValueService, "rolling_lines_by_espn_id",
                        staticmethod(fake_by_espn_id))

    lines = _roster_lines([_player(1, "A"), _player(2, "B")], FantasyProvider.ESPN)

    assert captured == {"ids": [1, 2], "days": 14}
    assert len(lines) == 1 and lines[0].pts == 20


@pytest.mark.unit
def test_roster_lines_yahoo_looks_up_by_name_and_team(monkeypatch):
    from services import team_insights_service

    captured = {}

    def fake_by_name(players, days=7):
        captured["players"], captured["days"] = list(players), days
        return {"a": StatLine(reb=5), "b": StatLine(reb=3)}

    monkeypatch.setattr(team_insights_service.PlayerValueService, "rolling_lines_by_name",
                        staticmethod(fake_by_name))

    lines = _roster_lines([_player(1, "A", "DEN"), _player(2, "B", "BOS")], FantasyProvider.YAHOO)

    assert captured == {"players": [("A", "DEN"), ("B", "BOS")], "days": 14}
    assert sum(line.reb for line in lines) == 8


def _fake_matchup(status=ApiStatus.SUCCESS, roster=None, message="ok"):
    data = SimpleNamespace(opponent_team=SimpleNamespace(roster=roster or [])) if roster is not None else None
    return SimpleNamespace(status=status, data=data, message=message)


@pytest.mark.unit
def test_opponent_comparison_uses_default_nine_cat_for_points_leagues(monkeypatch):
    from services import team_insights_service

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(team_insights_service, "run_db", direct_run_db)

    async def fake_matchup(user_id, team_id, avg_window="season"):
        assert (user_id, team_id) == (0, 7)
        return _fake_matchup(roster=[_player(11, "Opp A"), _player(12, "Opp B")])

    monkeypatch.setattr(team_insights_service.MatchupService, "get_matchup_by_team_id", staticmethod(fake_matchup))
    monkeypatch.setattr(
        team_insights_service.PlayerValueService, "rolling_lines_by_espn_id",
        staticmethod(lambda ids, days=7: {11: StatLine(pts=20, fgm=8, fga=20, tov=4), 12: StatLine(pts=15, fgm=3, fga=5, tov=1)}),
    )

    your_line = StatLine(pts=30, fgm=10, fga=20, tov=6)
    opp_strengths, comparison = asyncio.run(
        _opponent_comparison(7, your_line, FantasyProvider.ESPN, resolve_scoring(None))
    )

    assert opp_strengths is not None
    assert opp_strengths.avg_points == 35.0
    assert opp_strengths.avg_fg_pct == pytest.approx(11 / 25, abs=1e-4)
    assert comparison is not None
    assert [i.key for i in comparison.items] == ["fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"]
    winners = {i.key: i.winner for i in comparison.items}
    assert winners["fg_pct"] == "you"        # 0.50 vs 0.44
    assert winners["pts"] == "opp"           # 30 vs 35
    assert winners["tov"] == "opp"           # 6 vs 5: fewer turnovers wins
    assert winners["reb"] == "tie"
    assert (comparison.wins, comparison.losses, comparison.ties) == (1, 2, 6)


@pytest.mark.unit
def test_opponent_comparison_is_none_when_matchup_fails_or_raises(monkeypatch):
    from services import team_insights_service

    async def error_matchup(user_id, team_id, avg_window="season"):
        return _fake_matchup(status=ApiStatus.ERROR, message="ESPN unavailable")

    monkeypatch.setattr(team_insights_service.MatchupService, "get_matchup_by_team_id", staticmethod(error_matchup))
    assert asyncio.run(_opponent_comparison(7, StatLine(pts=1), FantasyProvider.ESPN, resolve_scoring(None))) == (None, None)

    async def boom(user_id, team_id, avg_window="season"):
        raise RuntimeError("network down")

    monkeypatch.setattr(team_insights_service.MatchupService, "get_matchup_by_team_id", staticmethod(boom))
    assert asyncio.run(_opponent_comparison(7, StatLine(pts=1), FantasyProvider.ESPN, resolve_scoring(None))) == (None, None)
