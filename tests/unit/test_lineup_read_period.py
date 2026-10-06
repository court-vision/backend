"""
LineupReadService.read for a requested ESPN day: what it asks ESPN for, which
period it treats as today, and the bounds it enforces.

The payloads follow a 2026-10-05 probe of a public league (426893737): with
`scoringPeriodId=N` ESPN echoes N as the top-level `scoringPeriodId`, keeps
`status.latestScoringPeriod` on today, and answers 200 for any N (an empty
roster for 0 or less, the last roster for days past `finalScoringPeriod`).
"""

import asyncio
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
import pytz

from core.errors import BadRequestError
from schemas.common import FantasyProvider, LeagueInfo
from services import lineup_read_service as svc
from utils.espn_helpers import PRO_TEAM_MAP, TEAM_ABBREV_CORRECTIONS

SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="Lvl. 3 Goblins", year=2027,
                    espn_s2="x", swid=SWID, espn_team_id=4)
OPENING = date(2026, 10, 20)
TODAY = 12                                                  # 2026-10-31
FINAL = 167
NOW = pytz.timezone("US/Eastern").localize(datetime(2026, 10, 31, 20, 0))
DEN = TEAM_ABBREV_CORRECTIONS.get(PRO_TEAM_MAP[7], PRO_TEAM_MAP[7])


def _day(period):
    return OPENING + timedelta(days=period - 1)


def _payload(requested, *, latest=TODAY, final=FINAL):
    entries = [{
        "playerId": 1, "lineupSlotId": 12, "injuryStatus": "ACTIVE",
        "playerPoolEntry": {"id": 1, "lineupLocked": False, "onTeamId": 4,
                            "player": {"id": 1, "fullName": "Player 1", "proTeamId": 7,
                                       "eligibleSlots": [0, 5, 11, 12], "injured": False,
                                       "injuryStatus": "ACTIVE", "defaultPositionId": 1}},
    }]
    status = {"firstScoringPeriod": 1}
    if final is not None:
        status["finalScoringPeriod"] = final
    if latest is not None:
        status["latestScoringPeriod"] = latest
    return {
        "scoringPeriodId": requested or latest,
        "status": status,
        "teams": [{"id": 4, "name": "Lvl. 3 Goblins", "owners": [SWID], "roster": {"entries": entries}}],
        "settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "11": 1, "12": 1},
                                        "lineupLocktimeType": "INDIVIDUAL_GAME"}},
    }


@pytest.fixture
def espn(monkeypatch):
    h = SimpleNamespace(calls=[], latest=TODAY, final=FINAL)

    async def fake_fetch(league_info, views, *, expect_key="teams", scoring_period_id=None):
        h.calls.append(scoring_period_id)
        return _payload(scoring_period_id, latest=h.latest, final=h.final)

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # A 7 PM DEN game on every day: tonight's has tipped (it is 8 PM), later ones have not.
    game = SimpleNamespace(home_team_id=DEN, away_team_id="LAL", start_time_et=time(19, 0))
    monkeypatch.setattr(svc.EspnService, "fetch_league", staticmethod(fake_fetch))
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_games_on", lambda nba_date: [game])
    monkeypatch.setattr(svc, "_values_for", lambda league_info, ids: ("fpts", {}, {}))
    monkeypatch.setattr(svc.schedule_service, "get_nba_today", lambda: _day(TODAY))
    monkeypatch.setattr(svc.schedule_service, "season_day", lambda d=None, season=None: (d - OPENING).days + 1)
    monkeypatch.setattr(svc.schedule_service, "date_for_espn_scoring_period", lambda p, season=None: _day(p))
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    return h


def read(period=None):
    return asyncio.run(svc.LineupReadService.read(1, LEAGUE, now=NOW, scoring_period_id=period))


@pytest.mark.unit
def test_todays_read_asks_espn_for_no_period_and_is_writable(espn):
    state = read()
    assert espn.calls == [None]
    assert (state.scoring_period_id, state.current_scoring_period_id, state.final_scoring_period_id) == (TODAY, TODAY, FINAL)
    assert state.nba_date == "2026-10-31" and state.scoring_period_source == "provider"
    assert state.can_write is True and state.write_blocked_reason is None and state.future is False
    assert state.players[0].game_started is True and state.players[0].locked is True


@pytest.mark.unit
def test_a_later_day_is_read_for_that_day(espn):
    state = read(TODAY + 3)
    assert espn.calls == [TODAY + 3]
    assert (state.scoring_period_id, state.current_scoring_period_id) == (TODAY + 3, TODAY)
    assert state.nba_date == "2026-11-03"
    assert state.future is True and state.can_write is True and state.write_blocked_reason is None
    p = state.players[0]
    assert p.has_game_today and p.opponent == "vs LAL" and p.game_time_et == "19:00"
    assert p.game_started is False and p.locked is False            # 7 PM on Nov 3 has not happened
    assert state.roster_version != read().roster_version             # a different day is a different board


@pytest.mark.unit
def test_asking_for_today_by_number_is_todays_board(espn):
    asked = read(TODAY)
    plain = read()
    assert asked.future is False and asked.can_write is True
    assert asked.roster_version == plain.roster_version and asked.nba_date == plain.nba_date


@pytest.mark.unit
@pytest.mark.parametrize("period, needle", [(TODAY - 1, "has passed"), (FINAL + 1, "last day")])
def test_days_outside_today_to_the_final_day_are_400(espn, period, needle):
    with pytest.raises(BadRequestError) as exc:
        read(period)
    assert exc.value.error_code == "SCORING_PERIOD_OUT_OF_RANGE" and needle in exc.value.message


@pytest.mark.unit
def test_without_espns_last_day_only_today_can_be_asked_for(espn):
    # No upper bound to check a later day against, and ESPN would answer day 300 too.
    espn.final = None
    assert read().scoring_period_id == TODAY and read(TODAY).scoring_period_id == TODAY
    with pytest.raises(BadRequestError) as exc:
        read(TODAY + 1)
    assert exc.value.error_code == "SCORING_PERIOD_OUT_OF_RANGE" and "last day" in exc.value.message


@pytest.mark.unit
def test_without_espns_today_the_calendar_decides_never_the_echo(espn):
    # The echo says 15; trusting it would make day 15 "today" and its edits go out as ROSTER.
    espn.latest = None
    state = read(TODAY + 3)
    assert state.current_scoring_period_id == TODAY and state.scoring_period_source == "calendar"
    assert state.future is True


@pytest.mark.unit
def test_a_calendar_failure_dates_the_board_by_its_offset_from_today(espn, monkeypatch):
    real = svc.schedule_service.date_for_espn_scoring_period

    def flaky(period, season=None):
        if period != TODAY:
            raise RuntimeError("no season bounds")
        return real(period)

    monkeypatch.setattr(svc.schedule_service, "date_for_espn_scoring_period", flaky)
    assert read(TODAY + 2).nba_date == "2026-11-02"
