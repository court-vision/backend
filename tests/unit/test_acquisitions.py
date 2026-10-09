"""
services.acquisitions: ESPN's add limits and a team's usage, from the mSettings / mTeam /
mTransactions2 shapes seen live on 2026-10-08 (the settings fixtures carry the same
`acquisitionSettings`), and the budget rule the scheduled pickups apply.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from schemas.lineup_editor import AcquisitionState
from services.acquisitions import (
    budget_block,
    count_adds,
    in_current_matchup,
    is_limit_refusal,
    matchup_room,
    parse_acquisitions,
    season_room,
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
def test_a_per_day_limit_from_the_settings_fixture():
    """The fixture's league (like every live league checked) allows one add a day, no season cap."""
    a = parse_acquisitions({**SETTINGS, "status": {"currentMatchupPeriod": 21}},
                           team(season=59, totals={"20": 7, "21": 14}))
    assert (a.per, a.limit, a.season_limit) == ("day", 1, None)
    assert (a.matchup_period_id, a.matchup_used, a.season_used) == (21, 14, 59)


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


@pytest.mark.unit
def test_a_per_day_limit_has_no_matchup_room():
    assert matchup_room(acq(per="day", limit=1)) is None


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
def test_a_per_day_limit_blocks_only_when_that_days_adds_are_used():
    daily = acq(per="day", limit=1)
    assert budget_block(daily, date(2026, 11, 20)) is None                 # not counted: ESPN decides
    assert budget_block(daily, date(2026, 11, 20), day_adds=0) is None
    assert budget_block(daily, date(2026, 11, 20), day_adds=1) == "That day's add is already used (1 of 1)"
    assert budget_block(acq(per="day", limit=2), date(2026, 11, 20), day_adds=2) == \
        "That day's adds are already used (2 of 2)"


@pytest.mark.unit
def test_in_current_matchup_is_inclusive():
    a = acq()
    assert in_current_matchup(a, date(2026, 11, 16)) and in_current_matchup(a, date(2026, 11, 22))
    assert not in_current_matchup(a, date(2026, 11, 15)) and not in_current_matchup(a, date(2026, 11, 23))


# ---- a day's adds -------------------------------------------------------------------------


def txn(tid, *, team_id=4, period=30, kind="FREEAGENT", status="EXECUTED", items=None):
    return {"id": tid, "teamId": team_id, "scoringPeriodId": period, "type": kind, "status": status,
            "items": items if items is not None else [{"type": "ADD", "toTeamId": team_id, "fromTeamId": 0},
                                                      {"type": "DROP", "toTeamId": 0, "fromTeamId": team_id}]}


@pytest.mark.unit
def test_count_adds_counts_executed_free_agent_and_waiver_adds_for_the_team_and_day():
    rows = [
        txn("a"), txn("b", kind="WAIVER"),
        txn("a"),                                   # listed under two days' reads: counted once
        txn("c", status="CANCELED"), txn("d", kind="ROSTER"), txn("e", kind="DRAFT"),
        txn("f", team_id=9), txn("g", period=31),
        txn("h", items=[{"type": "DROP", "toTeamId": 0, "fromTeamId": 4}]),     # a drop only
    ]
    assert count_adds(rows, 4, 30) == 2


@pytest.mark.unit
def test_count_adds_leaves_out_pre_season_adds():
    """ESPN records pre-season adds under day 1 but doesn't count them toward the limit."""
    pre = {**txn("p", period=1), "proposedDate": 1_759_700_000_000}         # 2025-10-05
    season = {**txn("s", period=1), "proposedDate": 1_792_000_000_000}      # 2026-10-14
    assert count_adds([pre, season], 4, 1) == 2
    assert count_adds([pre, season], 4, 1, since_ms=1_790_000_000_000) == 1


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
