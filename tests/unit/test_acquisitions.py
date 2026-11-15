"""
services.acquisitions: ESPN's add limits and a team's usage, from the mSettings / mTeam shapes
seen live on 2026-10-08 and 2026-11-15 (the settings fixtures carry the same
`acquisitionSettings`), the matchup total a per-game-day limit comes to, and the budget rule the
scheduled pickups apply.
"""

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from schemas.lineup_editor import AcquisitionState
from core.settings import settings
from services import acquisitions as mod
from services import schedule_service
from services.acquisitions import espn_game_days as REAL_ESPN_GAME_DAYS   # before the autouse stub
from services.acquisitions import (
    budget_block,
    in_current_matchup,
    is_limit_refusal,
    matchup_game_days,
    matchup_room,
    parse_acquisitions,
    parse_pro_schedule,
    season_room,
    with_game_days,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SETTINGS = json.loads((FIXTURES / "espn_settings_h2h_points.json").read_text())


def league(acq, *, matchup=5):
    return {"settings": {"acquisitionSettings": acq}, "status": {"currentMatchupPeriod": matchup}}


def team(*, season=0, totals=None):
    return {"id": 4, "transactionCounter": {"acquisitions": season, "matchupAcquisitionTotals": totals or {}}}


def acq(**kw):
    base = dict(per="matchup", limit=4, matchup_period_id=5, matchup_start=date(2026, 11, 16),
                matchup_end=date(2026, 11, 22), matchup_used=0, season_limit=None, season_used=0)
    base.update(kw)
    return AcquisitionState(**base)


# ---- parse ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_per_game_day_limit_from_the_settings_fixture():
    """The fixture's league (like every live league checked) allows one add for each day of the
    matchup with games, no season cap. Its total waits for the matchup's days."""
    a = parse_acquisitions({**SETTINGS, "status": {"currentMatchupPeriod": 21}},
                           team(season=59, totals={"20": 7, "21": 14}))
    assert (a.per, a.per_game_day, a.limit, a.season_limit) == ("game_day", 1, None, None)
    assert (a.matchup_period_id, a.matchup_used, a.season_used) == (21, 14, 59)
    assert matchup_room(a) is None                          # no total yet: nothing is refused over it


@pytest.mark.unit
def test_a_per_matchup_limit_and_a_season_cap():
    a = parse_acquisitions(league({"matchupAcquisitionLimit": 4.0, "matchupLimitPerScoringPeriod": False,
                                   "acquisitionLimit": 50}), team(season=12, totals={"4": 4, "5": 2}))
    assert (a.per, a.limit, a.matchup_used, a.season_limit, a.season_used) == ("matchup", 4, 2, 50, 12)
    assert (matchup_room(a), season_room(a)) == (2, 38)


@pytest.mark.unit
def test_minus_one_is_no_limit_and_no_settings_is_none():
    a = parse_acquisitions(league({"matchupAcquisitionLimit": -1.0, "matchupLimitPerScoringPeriod": False,
                                   "acquisitionLimit": -1}), team())
    assert (a.limit, a.season_limit, matchup_room(a), season_room(a)) == (None, None, None, None)
    assert parse_acquisitions({"settings": {}}, team()) is None


@pytest.mark.unit
def test_no_adds_yet_this_matchup_counts_zero():
    """Pre-season, or a matchup with no adds: the totals have no key for it."""
    a = parse_acquisitions(league({"matchupAcquisitionLimit": 1.0, "matchupLimitPerScoringPeriod": True}, matchup=1),
                           team())
    assert (a.matchup_period_id, a.matchup_used, a.season_used) == (1, 0, 0)


def per_game_day(n=1, **kw):
    return acq(per="game_day", limit=None, per_game_day=n, **kw)


@pytest.mark.unit
def test_a_per_game_day_limit_is_a_matchup_total_of_one_day_s_limit_per_day_with_games():
    """James's league, 2026-11-15: x/7 most weeks, x/6 in one with a day without games, all of it
    usable on any day (his team made three adds on Oct 30)."""
    week = with_game_days(per_game_day(1, matchup_used=4), 7)
    assert (week.limit, week.game_days, matchup_room(week)) == (7, 7, 3)
    assert with_game_days(per_game_day(1), 6).limit == 6
    assert with_game_days(per_game_day(2), 7).limit == 14
    assert with_game_days(per_game_day(1), None).limit is None
    assert with_game_days(acq(limit=4), 7) == acq(limit=4)          # a per-matchup limit is as set


@pytest.mark.unit
def test_the_pro_schedule_lists_espn_s_days_with_a_game():
    payload = {"settings": {"proTeams": [
        {"id": 1, "proGamesByScoringPeriod": {"1": [{"id": 9}], "2": [], "4": [{"id": 8}]}},
        {"id": 2, "proGamesByScoringPeriod": {"4": [{"id": 8}], "5": [{"id": 7}]}},
        {"id": 0},
    ]}}
    assert parse_pro_schedule(payload) == frozenset({1, 4, 5})
    assert parse_pro_schedule({}) == frozenset()


@pytest.fixture
def this_season(monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2026-27")
    schedule_service.reset_cache()
    yield
    schedule_service.reset_cache()


@pytest.mark.unit
def test_a_matchup_s_game_days_are_espn_s_count_when_its_schedule_reads(monkeypatch, this_season):
    """Opening night 2026-10-20 is ESPN day 1, so Nov 2-8 is days 14-20; ESPN had no game on Nov 3
    (day 15, Election Day)."""
    async def espn(year):
        assert year == 2027
        return frozenset(set(range(1, 30)) - {15})

    monkeypatch.setattr(mod, "espn_game_days", espn)
    assert asyncio.run(matchup_game_days(2027, date(2026, 11, 2), date(2026, 11, 8))) == 6
    assert asyncio.run(matchup_game_days(2027, date(2026, 11, 9), date(2026, 11, 15))) == 7


@pytest.mark.unit
def test_no_game_day_count_when_espn_s_schedule_can_t_be_read(this_season):
    """Our calendar doesn't stand in: it lacks games published since it was built (the Cup's
    December week), and a short count would refuse adds ESPN allows."""
    # The autouse stub answers None, as a failed read does
    assert asyncio.run(matchup_game_days(2027, date(2026, 11, 2), date(2026, 11, 8))) is None


@pytest.mark.unit
def test_espn_s_schedule_is_read_once_and_kept(monkeypatch):
    mod._pro_schedule.clear()
    calls = []

    async def provider_get(provider, url, **kw):
        calls.append(url)
        return {"settings": {"proTeams": [{"proGamesByScoringPeriod": {"3": [{"id": 1}]}}]}}

    import services.providers.http as http
    monkeypatch.setattr(http, "provider_get", provider_get)
    assert asyncio.run(REAL_ESPN_GAME_DAYS(2027)) == frozenset({3})
    assert asyncio.run(REAL_ESPN_GAME_DAYS(2027)) == frozenset({3})
    assert calls == ["https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027"]
    mod._pro_schedule.clear()


@pytest.mark.unit
def test_a_failed_read_of_espn_s_schedule_is_none_and_retried_later(monkeypatch):
    mod._pro_schedule.clear()

    async def down(provider, url, **kw):
        raise RuntimeError("ESPN timed out")

    import services.providers.http as http
    monkeypatch.setattr(http, "provider_get", down)
    assert asyncio.run(REAL_ESPN_GAME_DAYS(2027)) is None
    expires, days = mod._pro_schedule[2027]
    assert days is None and expires - mod.time.monotonic() <= mod.PRO_SCHEDULE_RETRY_S
    mod._pro_schedule.clear()


# ---- the budget ------------------------------------------------------------------------


@pytest.mark.unit
def test_nothing_blocks_an_add_with_room_or_without_settings():
    assert budget_block(acq(matchup_used=3), date(2026, 11, 20)) is None
    assert budget_block(None, date(2026, 11, 20)) is None


@pytest.mark.unit
def test_a_used_up_matchup_blocks_its_own_days_only():
    used = acq(matchup_used=4)
    assert budget_block(used, date(2026, 11, 20)) == "Every add this matchup allows is used (4 of 4)"
    # The next matchup's first day may count toward that matchup instead.
    assert budget_block(used, date(2026, 11, 23)) is None
    # Without the matchup's days the rule can't tell: it lets ESPN decide.
    assert budget_block(used.model_copy(update={"matchup_start": None, "matchup_end": None}), date(2026, 11, 20)) is None


@pytest.mark.unit
def test_a_used_up_season_blocks_every_day():
    assert budget_block(acq(season_limit=40, season_used=40), date(2026, 12, 25)) == \
        "Every add the league allows this season is used (40 of 40)"


@pytest.mark.unit
def test_a_per_game_day_limit_allows_several_adds_in_a_day_until_the_matchup_s_are_used():
    week = with_game_days(per_game_day(1, matchup_used=3), 7)
    assert budget_block(week, date(2026, 11, 20)) is None                  # three made, all on one day
    used = week.model_copy(update={"matchup_used": 7})
    assert budget_block(used, date(2026, 11, 20)) == "Every add this matchup allows is used (7 of 7)"
    assert budget_block(per_game_day(1, matchup_used=7), date(2026, 11, 20)) is None   # no total known


@pytest.mark.unit
def test_pickups_still_scheduled_this_matchup_take_its_adds_too():
    left_two = acq(limit=4, matchup_used=2)
    assert budget_block(left_two, date(2026, 11, 20), pending=[date(2026, 11, 18)]) is None
    assert budget_block(left_two, date(2026, 11, 20), pending=[date(2026, 11, 18), date(2026, 11, 21)]) == \
        "Every add this matchup allows is used (2 of 4), with 2 more scheduled"
    # One for next matchup's day counts there, not here
    assert budget_block(left_two, date(2026, 11, 20), pending=[date(2026, 11, 18), date(2026, 11, 24)]) is None
    # The season's cap counts every one
    capped = acq(limit=None, season_limit=10, season_used=9)
    assert budget_block(capped, date(2026, 11, 20), pending=[date(2026, 12, 1)]) == \
        "Every add the league allows this season is used (9 of 10), with 1 more scheduled"


@pytest.mark.unit
def test_in_current_matchup_is_inclusive():
    a = acq()
    assert in_current_matchup(a, date(2026, 11, 16)) and in_current_matchup(a, date(2026, 11, 22))
    assert not in_current_matchup(a, date(2026, 11, 15)) and not in_current_matchup(a, date(2026, 11, 23))


# ---- ESPN's refusal ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("code, message, expected", [
    ("TRAN_ACQUISITION_LIMIT_EXCEEDED", "", True),
    (None, "You have reached the maximum number of acquisitions for this matchup.", True),
    ("TRAN_ROSTER_POSITION_LIMIT_EXCEEDED", "Too many players with default position C (maximum 4)", False),
    ("TRAN_ROSTER_FULL", "Roster is full.", False),
    (None, None, False),
])
def test_is_limit_refusal_reads_the_words_not_any_limit(code, message, expected):
    assert is_limit_refusal(code, message) is expected
