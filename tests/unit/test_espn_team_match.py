"""A team renamed on ESPN is still the user's team (find_espn_team)."""

import pytest

from schemas.common import LeagueInfo
from services.espn_service import EspnService
from services.espn_team_match import find_espn_team

SWID = "{AAAAAAAA-1111-2222-3333-444444444444}"
TEAMS = [
    {"id": 3, "name": "Someone Else", "owners": ["{BBBBBBBB-1111-2222-3333-444444444444}"],
     "roster": {"entries": []}},
    {"id": 7, "name": "New Name", "owners": [SWID], "roster": {"entries": [{"playerId": 1}]}},
]


def test_finds_by_id_before_name():
    assert find_espn_team(TEAMS, team_name="Someone Else", espn_team_id=7)["id"] == 7


def test_finds_by_name():
    assert find_espn_team(TEAMS, team_name="  New Name ")["id"] == 7


def test_renamed_team_found_by_owner_swid():
    swid = SWID.strip("{}").lower()  # any spelling of the cookie
    assert find_espn_team(TEAMS, team_name="Old Name", swid=swid)["id"] == 7


def test_ambiguous_owner_is_not_a_match():
    teams = TEAMS + [{"id": 9, "name": "Second Team", "owners": [SWID]}]
    assert find_espn_team(teams, team_name="Old Name", swid=SWID) is None


def test_unknown_team_without_swid():
    assert find_espn_team(TEAMS, team_name="Old Name") is None


def test_resolve_team_brings_league_info_up_to_date():
    info = LeagueInfo(league_id=1, team_name="Old Name", year=2026, swid=SWID, espn_s2="x")
    assert EspnService.get_roster(info, TEAMS) == [{"playerId": 1}]
    assert (info.espn_team_id, info.team_name) == (7, "New Name")


@pytest.mark.parametrize("stored_id", [None, 7])
def test_resolve_team_keeps_id_when_found(stored_id):
    info = LeagueInfo(league_id=1, team_name="New Name", year=2026, espn_team_id=stored_id)
    assert EspnService.resolve_team(info, TEAMS)["id"] == 7
    assert info.espn_team_id == 7
