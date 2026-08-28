"""find_streamers: mode weights vs league point weights, ET day, and Yahoo name keys."""

import asyncio
from datetime import date

import pytest

from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp
from schemas.streamer import StreamerMode
from services import streamer_service as ss
from services.player_service import _normalize_name
from services.scoring.resolver import ResolvedScoring
from services.streamer_service import StreamerService

MATCHUP = {
    "matchup_number": 3,
    "game_span": 7,
    "start_date": date(2026, 11, 2),
    "end_date": date(2026, 11, 8),
    "current_day_index": 2,
    "games": {"DEN": {"2": True, "3": True, "5": True}, "LAL": {"4": True}},
}


def _fa(pid, name, team, avg=10.0, injured=False):
    return PlayerResp(player_id=pid, name=name, avg_points=avg, team=team,
                      valid_positions=["PG"], injured=injured, injury_status=None)


class _NoRows:
    def where(self, *_):
        return []


@pytest.fixture
def stubbed(monkeypatch):
    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(ss, "run_db", direct_run_db)
    monkeypatch.setattr(ss, "get_current_matchup", lambda *a, **k: MATCHUP)
    monkeypatch.setattr(ss, "get_nba_today", lambda: date(2026, 11, 4))
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: [2, 3, 5] if team == "DEN" else [4])
    monkeypatch.setattr(ss, "get_remaining_games", lambda team, d: 3 if team == "DEN" else 1)
    monkeypatch.setattr(ss, "has_remaining_b2b", lambda team, d: team == "DEN")
    monkeypatch.setattr(ss, "get_b2b_game_count", lambda team, d: 2 if team == "DEN" else 0)
    monkeypatch.setattr(ss, "get_teams_with_b2b", lambda d: ["DEN"])
    monkeypatch.setattr(ss.PlayerModel, "select", classmethod(lambda cls, *a, **k: _NoRows()))
    # A points league with its own weights (no DB): the dispatcher must pass them to the value service
    monkeypatch.setattr(ss.PlayerValueService, "scoring_for",
                        staticmethod(lambda li, team_id=None: ResolvedScoring("points", None, True, {"pts": 1.0})))


@pytest.mark.unit
def test_week_mode_scores_with_mode_weights_not_point_weights(stubbed, monkeypatch):
    fas = [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)]

    async def fake_fas(li, count):
        return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=fas)

    captured = {}

    def fake_avgs(ids, days, weights):
        captured["weights"] = weights
        return {1: 50.0, 2: 10.0}

    monkeypatch.setattr(ss.EspnService, "get_free_agents", staticmethod(fake_fas))
    monkeypatch.setattr(ss.PlayerValueService, "rolling_avg_by_espn_id", staticmethod(fake_avgs))

    li = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027)
    resp = asyncio.run(StreamerService.find_streamers(li, fa_count=10, mode=StreamerMode.WEEK, avg_days=7))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert captured["weights"] == {"pts": 1.0}                 # league point weights reach the value service
    by_id = {s.player_id: s for s in resp.data.streamers}
    w = StreamerService.WEEK_WEIGHTS
    assert by_id[1].streamer_score == round(w["b2b"] + 3 * w["games_remaining"] + 50.0 * w["avg_points"] + 2 * w["b2b_games"], 1)
    assert by_id[2].streamer_score == round(1 * w["games_remaining"] + 10.0 * w["avg_points"], 1)
    assert resp.data.streamers[0].player_id == 1              # B2B teams sort first in week mode


@pytest.mark.unit
def test_daily_mode_uses_daily_weights_and_target_day(stubbed, monkeypatch):
    fas = [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)]

    async def fake_fas(li, count):
        return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=fas)

    monkeypatch.setattr(ss.EspnService, "get_free_agents", staticmethod(fake_fas))
    monkeypatch.setattr(ss.PlayerValueService, "rolling_avg_by_espn_id", staticmethod(lambda ids, days, weights: {1: 50.0, 2: 10.0}))

    li = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027)
    resp = asyncio.run(StreamerService.find_streamers(li, mode=StreamerMode.DAILY, target_day=2))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert [s.player_id for s in resp.data.streamers] == [1]   # only DEN plays on day 2
    w = StreamerService.DAILY_WEIGHTS
    assert resp.data.streamers[0].streamer_score == round(w["b2b"] + 3 * w["games_remaining"] + 50.0 * w["avg_points"] + 2 * w["b2b_games"], 1)


@pytest.mark.unit
def test_yahoo_accented_names_resolve_their_average(stubbed, monkeypatch):
    fas = [_fa(1, "Nikola Jokić", "DEN", 55.0)]

    async def fake_fas(li, count, team_id):
        return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=fas)

    def fake_by_name(pairs, days, weights):
        return {_normalize_name(name): 48.5 for name, _ in pairs}   # keyed like the real service

    monkeypatch.setattr(ss.YahooService, "get_free_agents", staticmethod(fake_fas))
    monkeypatch.setattr(ss.PlayerValueService, "rolling_avg_by_name", staticmethod(fake_by_name))

    li = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027, yahoo_team_key="466.l.1.t.1")
    resp = asyncio.run(StreamerService.find_streamers(li, mode=StreamerMode.WEEK, team_id=5))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert resp.data.streamers[0].avg_points_last_n == 48.5
