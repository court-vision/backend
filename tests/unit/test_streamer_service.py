"""find_streamers: mode weights vs league point weights, ET day, pre-season week 1, and Yahoo name keys."""

import asyncio
from datetime import date, timedelta

import pytest

from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp
from schemas.streamer import StreamerMode
from services import streamer_service as ss
from services.player_service import _normalize_name
from services.player_value_service import ValueResult
from services.scoring.models import StatLine
from services.scoring.resolver import ResolvedScoring
from services.streamer_service import StreamerService
from services.providers import identity

MATCHUP = {
    "matchup_number": 3,
    "game_span": 7,
    "start_date": date(2026, 11, 2),
    "end_date": date(2026, 11, 8),
    "current_day_index": 2,
    "games": {"DEN": {"2": True, "3": True, "5": True}, "LAL": {"4": True}},
    "upcoming": False,
}
# Week 1 seen from September: the picks are for a week that has not started
UPCOMING = {
    "matchup_number": 1,
    "game_span": 6,
    "start_date": date(2026, 10, 20),
    "end_date": date(2026, 10, 25),
    "current_day_index": 0,
    "games": {"DEN": {"0": True, "1": True, "4": True}, "LAL": {"2": True}},
    "upcoming": True,
}


def _fa(pid, name, team, avg=10.0, injured=False, **extra):
    return PlayerResp(player_id=pid, name=name, avg_points=avg, team=team,
                      valid_positions=["PG"], injured=injured, injury_status=None, **extra)


class _NoRows:
    def where(self, *_):
        return []


@pytest.fixture
def stubbed(monkeypatch):
    """No DB, no calendar files: the matchup, the schedule helpers and the value lookup are stubbed."""
    state = {"value_calls": [], "values": {1: ValueResult(50.0, "rolling"), 2: ValueResult(10.0, "rolling")}}

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_avg_points_for(scoring, *, espn_ids=None, names=None, days=14, recent=False):
        state["value_calls"].append((scoring, espn_ids, names, days))
        if names is not None:
            return {_normalize_name(name): ValueResult(48.5, "rolling") for name, _ in names}
        return {eid: state["values"].get(eid, ValueResult(None, None)) for eid in espn_ids}

    monkeypatch.setattr(ss, "run_db", direct_run_db)
    monkeypatch.setattr(ss, "get_streaming_matchup", lambda *a, **k: dict(MATCHUP))
    monkeypatch.setattr(ss, "get_nba_today", lambda: date(2026, 11, 4))
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: [2, 3, 5] if team == "DEN" else [4])
    monkeypatch.setattr(ss, "get_remaining_games", lambda team, d: 3 if team == "DEN" else 1)
    monkeypatch.setattr(ss, "has_remaining_b2b", lambda team, d: team == "DEN")
    monkeypatch.setattr(ss, "get_b2b_game_count", lambda team, d: 2 if team == "DEN" else 0)
    monkeypatch.setattr(ss, "get_teams_with_b2b", lambda d: ["DEN"])
    monkeypatch.setattr(identity.PlayerModel, "select", classmethod(lambda cls, *a, **k: _NoRows()))
    # A points league with its own weights (no DB): the dispatcher must pass them to the value service
    monkeypatch.setattr(ss.PlayerValueService, "scoring_for",
                        staticmethod(lambda li, team_id=None: ResolvedScoring("points", None, True, {"pts": 1.0})))
    monkeypatch.setattr(ss.PlayerValueService, "avg_points_for", staticmethod(fake_avg_points_for))
    return state


def _espn_fas(monkeypatch, fas):
    async def fake_fas(li, count):
        return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=fas)
    monkeypatch.setattr(ss.EspnService, "get_free_agents", staticmethod(fake_fas))


ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027)


@pytest.mark.unit
def test_week_mode_scores_with_mode_weights_not_point_weights(stubbed, monkeypatch):
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)])

    resp = asyncio.run(StreamerService.find_streamers(ESPN, fa_count=10, mode=StreamerMode.WEEK, avg_days=7))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    scoring, espn_ids, _, days = stubbed["value_calls"][0]
    assert scoring.point_weights == {"pts": 1.0} and espn_ids == [1, 2] and days == 7   # league weights reach the value service
    by_id = {s.player_id: s for s in resp.data.streamers}
    w = StreamerService.WEEK_WEIGHTS
    assert by_id[1].streamer_score == round(w["b2b"] + 3 * w["games_remaining"] + 50.0 * w["avg_points"] + 2 * w["b2b_games"], 1)
    assert by_id[2].streamer_score == round(1 * w["games_remaining"] + 10.0 * w["avg_points"], 1)
    assert resp.data.streamers[0].player_id == 1              # B2B teams sort first in week mode
    assert (resp.data.start_date, resp.data.end_date, resp.data.upcoming) == (date(2026, 11, 2), date(2026, 11, 8), False)


@pytest.mark.unit
def test_daily_mode_uses_daily_weights_and_target_day(stubbed, monkeypatch):
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)])

    resp = asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.DAILY, target_day=2))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert [s.player_id for s in resp.data.streamers] == [1]   # only DEN plays on day 2
    w = StreamerService.DAILY_WEIGHTS
    assert resp.data.streamers[0].streamer_score == round(w["b2b"] + 3 * w["games_remaining"] + 50.0 * w["avg_points"] + 2 * w["b2b_games"], 1)


@pytest.mark.unit
def test_week_mode_values_from_today_never_before_the_week_starts(stubbed, monkeypatch):
    """Mid-week the schedule helpers see the ET fantasy day, not the week's start."""
    seen = []
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: seen.append(d) or [2, 3, 5])
    monkeypatch.setattr(ss, "get_teams_with_b2b", lambda d: seen.append(d) or ["DEN"])
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0)])

    asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.WEEK))
    assert set(seen) == {date(2026, 11, 4)}


@pytest.mark.unit
def test_pre_season_picks_are_for_week_one_valued_from_opening_night(stubbed, monkeypatch):
    """Before opening night the matchup is week 1 (upcoming) and every schedule helper is asked
    about its first day, so the whole week counts — not today's date, which has no games."""
    monkeypatch.setattr(ss, "get_streaming_matchup", lambda *a, **k: dict(UPCOMING))
    monkeypatch.setattr(ss, "get_nba_today", lambda: date(2026, 9, 9))
    seen = []
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: seen.append(d) or ([0, 1, 4] if team == "DEN" else [2]))
    monkeypatch.setattr(ss, "get_remaining_games", lambda team, d: seen.append(d) or (3 if team == "DEN" else 1))
    monkeypatch.setattr(ss, "has_remaining_b2b", lambda team, d: seen.append(d) or team == "DEN")
    monkeypatch.setattr(ss, "get_b2b_game_count", lambda team, d: seen.append(d) or (2 if team == "DEN" else 0))
    monkeypatch.setattr(ss, "get_teams_with_b2b", lambda d: seen.append(d) or ["DEN"])
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)])

    resp = asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.WEEK))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert seen and set(seen) == {date(2026, 10, 20)}          # effective_date == start_date
    d = resp.data
    assert (d.matchup_number, d.current_day_index, d.game_span) == (1, 0, 6)
    assert (d.start_date, d.end_date, d.upcoming) == (date(2026, 10, 20), date(2026, 10, 25), True)
    assert [s.player_id for s in d.streamers] == [1, 2] and d.streamers[0].games_remaining == 3


@pytest.mark.unit
def test_pre_season_daily_mode_targets_days_of_week_one(stubbed, monkeypatch):
    monkeypatch.setattr(ss, "get_streaming_matchup", lambda *a, **k: dict(UPCOMING))
    monkeypatch.setattr(ss, "get_nba_today", lambda: date(2026, 9, 9))
    seen = []
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: seen.append(d) or ([0, 1, 4] if team == "DEN" else [2]))
    monkeypatch.setattr(ss, "get_remaining_games", lambda team, d: 3 if team == "DEN" else 1)
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0)])

    resp = asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.DAILY))   # defaults to day 0
    assert resp.data.target_day == 0 and [s.player_id for s in resp.data.streamers] == [1]
    assert set(seen) == {date(2026, 10, 20)}

    resp = asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.DAILY, target_day=2))
    assert [s.player_id for s in resp.data.streamers] == [2] and resp.data.streamers[0].has_b2b is False
    assert date(2026, 10, 22) in seen


@pytest.mark.unit
def test_no_matchup_on_the_calendar_is_an_empty_success(stubbed, monkeypatch):
    monkeypatch.setattr(ss, "get_streaming_matchup", lambda *a, **k: None)
    called = []
    _espn_fas(monkeypatch, [])
    monkeypatch.setattr(ss.EspnService, "get_free_agents", staticmethod(lambda *a, **k: called.append(1)))

    resp = asyncio.run(StreamerService.find_streamers(ESPN))
    assert resp.status == ApiStatus.SUCCESS and resp.data is None and called == []
    assert resp.message == "No matchup on the calendar — streaming picks return with the new season"


@pytest.mark.unit
def test_yahoo_accented_names_resolve_their_average(stubbed, monkeypatch):
    fas = [_fa(1, "Nikola Jokić", "DEN", 55.0)]

    async def fake_fas(li, count, team_id):
        return TeamDataResp(status=ApiStatus.SUCCESS, message="ok", data=fas)

    monkeypatch.setattr(ss.YahooService, "get_free_agents", staticmethod(fake_fas))

    li = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027, yahoo_team_key="466.l.1.t.1")
    resp = asyncio.run(StreamerService.find_streamers(li, mode=StreamerMode.WEEK, team_id=5))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert resp.data.streamers[0].avg_points_last_n == 48.5
    _, espn_ids, names, _ = stubbed["value_calls"][0]
    assert espn_ids is None and names == [("Nikola Jokić", "DEN")]   # Yahoo values are looked up by name


# ---- the live prices (the Live rankings plan, step 8) ----------------------------------------


def _live(pid, espn_id, team, timeline, rates=None):
    from services import live_projection

    return live_projection.LivePlayer(
        player_id=pid, name=f"P{pid}", espn_id=espn_id, name_normalized=f"p{pid}", team=team,
        line=StatLine(pts=10.0, min=20.0, gp=1.0), games=0.0, timeline=live_projection.parse_timeline(timeline),
        rates=rates or {"pts": 0.5},
    )


def _priced(monkeypatch, live_prices, players, nba_ids):
    """The morning's projection carries `players`; the pool's ESPN ids resolve through `nba_ids`."""
    week = [date(2026, 11, 2) + timedelta(days=d) for d in range(7)]
    live_prices(players, {"DEN": [week[2], week[3], week[5]], "LAL": [week[4]]}, as_of=date(2026, 11, 4))
    from services.providers.adapters import EspnAdapter

    monkeypatch.setattr(EspnAdapter, "nba_ids", lambda self, rows: {r.player_id: nba_ids[r.player_id]
                                                                    for r in rows if r.player_id in nba_ids})


@pytest.mark.unit
def test_each_streamer_is_priced_on_his_own_nights_off_the_live_projection(stubbed, monkeypatch, live_prices):
    """Jokić's backup starts tonight and tomorrow while the starter is out, then goes back to
    the bench: tonight he is a pickup, by Saturday he is not."""
    _espn_fas(monkeypatch, [_fa(1, "Backup Big", "DEN", 55.0), _fa(2, "Role Player", "LAL", 12.0),
                            _fa(3, "Not Carried", "LAL", 12.0)])
    _priced(monkeypatch, live_prices, [
        _live(101, 1, "DEN", [["2026-11-04", "2026-11-05", 2, 1.0, 34.0, 1.0], ["2026-11-07", "2026-12-31", 20, 0.9, 12.0, 1.0]]),
        _live(102, 2, "LAL", [["2026-11-04", "2026-12-31", 20, 0.5, 20.0, 1.0]]),
    ], {1: 101, 2: 102, 3: 103})

    resp = asyncio.run(StreamerService.find_streamers(ESPN, fa_count=10, mode=StreamerMode.WEEK, avg_days=7))

    by_id = {s.player_id: s for s in resp.data.streamers}
    assert by_id[1].game_days == [2, 3, 5] and by_id[1].live_values == [17.0, 17.0, 5.4]
    assert (by_id[1].live_day, by_id[1].live_week) == (17.0, 39.4)
    assert (by_id[2].live_values, by_id[2].live_day, by_id[2].live_week) == ([5.0], None, 5.0)   # no game today
    assert (by_id[3].live_values, by_id[3].live_day, by_id[3].live_week) == (None, None, None)   # not carried
    assert resp.data.live_as_of == date(2026, 11, 4)
    # the list's own order and score still come off the stored averages
    w = StreamerService.WEEK_WEIGHTS
    assert by_id[1].streamer_score == round(w["b2b"] + 3 * w["games_remaining"] + 50.0 * w["avg_points"] + 2 * w["b2b_games"], 1)


@pytest.mark.unit
def test_the_one_day_search_prices_its_own_day(stubbed, monkeypatch, live_prices):
    _espn_fas(monkeypatch, [_fa(1, "Backup Big", "DEN", 55.0)])
    _priced(monkeypatch, live_prices, [
        _live(101, 1, "DEN", [["2026-11-04", "2026-11-05", 2, 1.0, 34.0, 1.0], ["2026-11-07", "2026-12-31", 20, 0.9, 12.0, 1.0]]),
    ], {1: 101})
    monkeypatch.setattr(ss, "get_remaining_game_days", lambda team, d: [5])

    resp = asyncio.run(StreamerService.find_streamers(ESPN, mode=StreamerMode.DAILY, target_day=5))

    (s,) = resp.data.streamers
    assert (s.live_day, s.live_values, s.live_week) == (5.4, [5.4], 5.4)        # Saturday's, not tonight's


@pytest.mark.unit
def test_a_streamer_the_projection_has_on_another_schedule_keeps_his_averages_alone(stubbed, monkeypatch, live_prices):
    """Traded this morning: ESPN lists him on DEN, the projection still prices LAL's nights."""
    _espn_fas(monkeypatch, [_fa(1, "Just Traded", "DEN", 55.0)])
    _priced(monkeypatch, live_prices, [_live(101, 1, "LAL", [["2026-11-04", "2026-12-31", 20, 1.0, 30.0, 1.0]])],
            {1: 101})

    resp = asyncio.run(StreamerService.find_streamers(ESPN, fa_count=10, mode=StreamerMode.WEEK, avg_days=7))

    (s,) = resp.data.streamers
    assert (s.live_values, s.live_day, s.live_week) == (None, None, None)
    assert resp.data.live_as_of is None and s.avg_points_last_n == 50.0


@pytest.mark.unit
def test_without_a_projection_the_list_is_the_one_it_was(stubbed, monkeypatch):
    _espn_fas(monkeypatch, [_fa(1, "Nikola Jokić", "DEN", 55.0)])
    resp = asyncio.run(StreamerService.find_streamers(ESPN, fa_count=10, mode=StreamerMode.WEEK, avg_days=7))
    (s,) = resp.data.streamers
    assert (s.live_values, s.live_day, s.live_week, resp.data.live_as_of) == (None, None, None, None)
