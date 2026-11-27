"""
services.espn_trim: the filters that ask ESPN's league endpoint for only what a
read uses, and the checks that a filtered answer is the whole answer. Shapes
follow a 2026-11-27 probe of a public league (426893737).
"""

import json

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from services import espn_trim

LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=7, team_name="Mine", year=2027, espn_team_id=1)


def _payload(period=3, *, rosters=(1, 2), schedule=((3, 1, 2), (3, 4, 5))):
    return {
        "status": {"currentMatchupPeriod": period},
        "teams": [{"id": i, "name": f"T{i}", **({"roster": {"entries": []}} if i in rosters else {})} for i in range(1, 6)],
        "schedule": [{"matchupPeriodId": p, "home": {"teamId": h}, "away": {"teamId": a}} for p, h, a in schedule],
    }


@pytest.mark.unit
def test_the_filters_are_what_espn_takes():
    assert espn_trim.roster_params([5, 2, 5]) == {"rosterForTeamId": [2, 5]}
    assert json.loads(espn_trim.schedule_filter(6, 4)["x-fantasy-filter"]) == {
        "schedule": {"filterMatchupPeriodIds": {"value": [6]}, "filterTeamIds": {"value": [4]}}}
    assert espn_trim.season_totals(2027) == {
        "filterStatsForExternalIds": {"value": [2027]},
        "filterStatsForSourceIds": {"value": [0]},
        "filterStatsForSplitTypeIds": {"value": [0]},
    }


@pytest.mark.unit
def test_rosters_included_needs_every_named_team_with_its_roster():
    assert espn_trim.rosters_included(_payload(), [1, 2])
    assert not espn_trim.rosters_included(_payload(rosters=()), [1])      # an id ESPN didn't know: no rosters at all
    assert not espn_trim.rosters_included(_payload(), [1, 3])             # 3 is in the league, without its roster
    assert not espn_trim.rosters_included(_payload(), [9])                # not in the league
    assert not espn_trim.rosters_included(_payload(), [])                 # nothing asked is nothing proved


@pytest.mark.unit
def test_a_pair_holds_only_for_the_same_period_and_the_same_two_teams():
    pair = espn_trim.MatchupPair(matchup_period_id=3, ours=1, opponent=2)
    assert espn_trim.pair_holds(_payload(), pair)
    assert not espn_trim.pair_holds(_payload(period=4), pair)                           # the week turned
    assert not espn_trim.pair_holds(_payload(rosters=(1,)), pair)                       # a roster missing
    assert not espn_trim.pair_holds(_payload(rosters=(1, 2, 4), schedule=((3, 1, 4),)), pair)  # another opponent
    assert not espn_trim.pair_holds(_payload(schedule=((3, 4, 5),)), pair)              # no matchup of ours


@pytest.mark.unit
def test_a_pair_is_remembered_per_league_season_and_team():
    assert espn_trim.recall_pair(LEAGUE) is None
    espn_trim.remember_pair(LEAGUE, 3, 1, 2)
    assert espn_trim.recall_pair(LEAGUE) == espn_trim.MatchupPair(3, 1, 2)
    assert espn_trim.recall_pair(LEAGUE.model_copy(update={"espn_team_id": 2})) is None
    assert espn_trim.recall_pair(LEAGUE.model_copy(update={"year": 2028})) is None
    assert espn_trim.recall_pair(LEAGUE.model_copy(update={"espn_team_id": None})) is None
    espn_trim.remember_pair(LEAGUE, 3, 1, None)                            # nothing to remember: the old pair stays
    assert espn_trim.recall_pair(LEAGUE) == espn_trim.MatchupPair(3, 1, 2)
