"""
Integration: a saved ESPN team moving to the league's next season.

ESPN renews a league under the same league id and team ids, and Court Vision's
team identity (league + name, `team_identifier`) has no season. Adding the
renewal's team -- from the account's list or by league id -- used to answer
"already exists" with last season's team, or hide the entry as tracked, so a
returning user could not get this season's team in at all. Adding it now moves
the saved team: same team id, this season's year, name and ESPN id, and this
season's league settings.
"""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from db.models.leagues import League
from db.models.teams import Team
from db.models.users import User
from schemas.common import FantasyProvider, LeagueInfo
from services.league_service import LeagueService
from services.team_service import TeamService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SETTINGS = json.loads((FIXTURES / "espn_settings_h2h_category.json").read_text())
LEAGUE = 1234567
SWID = "{3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6}"
OTHER = "{00000000-AAAA-BBBB-CCCC-DDDDDDDDDDDD}"


@pytest.fixture
def user(integration_db):
    return User.create(email="seasons@courtvision.dev", clerk_user_id="user_seasons", created_at=datetime.utcnow())


@pytest.fixture
def espn(monkeypatch):
    """ESPN's league read (mTeam + mSettings), one payload per season; records the seasons read."""
    from services import espn_service as module

    state = {"teams": {}, "reads": []}

    async def fake(provider, url, **kwargs):
        season = int(url.split("/seasons/")[1].split("/")[0])
        state["reads"].append(season)
        payload = copy.deepcopy(SETTINGS)
        payload["id"], payload["seasonId"] = LEAGUE, season
        payload["teams"] = state["teams"][season]
        return payload

    monkeypatch.setattr(module, "provider_get", fake)
    return state


def _saved(user, name, year, espn_team_id=None, **extra):
    info = LeagueInfo(provider=FantasyProvider.ESPN, league_id=LEAGUE, team_name=name, year=year,
                      league_name="The League", espn_team_id=espn_team_id)
    stored = {**json.loads(TeamService.serialize_league_info(info)), **extra}
    return Team.create(user_id=user.user_id, team_identifier=f"{LEAGUE}{name}", league_info=json.dumps(stored))


def _request(name, year, **overrides):
    body = dict(provider=FantasyProvider.ESPN, league_id=LEAGUE, team_name=name, year=year,
                espn_s2="AEB-COOKIE", swid=SWID)
    body.update(overrides)
    return LeagueInfo(**body)


def _info(team_id) -> dict:
    return json.loads(Team.get_by_id(team_id).league_info)


async def test_adding_this_season_moves_last_seasons_team(user, espn):
    last = _saved(user, "Alpha", 2026)
    espn["teams"][2026] = [{"id": 1, "name": "Alpha", "owners": [SWID]}]
    espn["teams"][2027] = [{"id": 1, "name": "Alpha", "owners": [SWID]}, {"id": 2, "name": "Bravo", "owners": [OTHER]}]
    await LeagueService.sync_for_team(last.team_id, _request("Alpha", 2026))
    assert League.get_by_id(Team.get_by_id(last.team_id).league_id).season == 2026

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2027))

    assert (resp.team_id, resp.already_exists, resp.moved_from_season) == (last.team_id, False, 2026)
    assert espn["reads"][-1] == 2027
    info = _info(last.team_id)
    assert (info["year"], info["team_name"], info["espn_team_id"], info["league_name"]) == (2027, "Alpha", 1, "The League")
    league = League.get_by_id(Team.get_by_id(last.team_id).league_id)
    assert (league.provider_league_id, league.season) == (str(LEAGUE), 2027)
    assert Team.select().where(Team.user_id == user.user_id).count() == 1


async def test_a_team_renamed_before_its_espn_id_was_learned_moves_too(user, espn):
    """The friend's case: last season's team, saved by its old name with no ESPN
    id, renamed on ESPN since. ESPN's teams for this season name the one the
    account's SWID owns."""
    last = _saved(user, "Old Name", 2026)
    espn["teams"][2027] = [{"id": 2, "name": "Bravo", "owners": [OTHER]}, {"id": 4, "name": "New Name", "owners": [SWID]}]

    resp = await TeamService.add_team(user.user_id, _request("New Name", 2027))

    assert (resp.team_id, resp.moved_from_season) == (last.team_id, 2026)
    team = Team.get_by_id(last.team_id)
    assert team.team_identifier == f"{LEAGUE}New Name"
    assert (_info(team.team_id)["team_name"], _info(team.team_id)["espn_team_id"]) == ("New Name", 4)
    assert Team.select().where(Team.user_id == user.user_id).count() == 1


async def test_a_renamed_team_with_a_learned_espn_id_moves_by_the_id(user, espn):
    last = _saved(user, "Old Name", 2026, espn_team_id=4)
    espn["teams"][2027] = [{"id": 4, "name": "New Name", "owners": [OTHER]}]   # owners need not say so

    resp = await TeamService.add_team(user.user_id, _request("New Name", 2027))

    assert (resp.team_id, resp.moved_from_season) == (last.team_id, 2026)
    assert Team.select().where(Team.user_id == user.user_id).count() == 1


async def test_adding_the_teams_own_season_again_only_finds_it(user, espn):
    current = _saved(user, "Alpha", 2027, espn_team_id=1)
    espn["teams"][2027] = [{"id": 1, "name": "Alpha", "owners": [SWID]}]

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2027))

    assert (resp.team_id, resp.already_exists, resp.moved_from_season) == (current.team_id, True, None)


async def test_a_renamed_team_added_again_in_its_season_is_not_duplicated(user, espn):
    current = _saved(user, "Old Name", 2027, espn_team_id=4)
    espn["teams"][2027] = [{"id": 4, "name": "New Name", "owners": [SWID]}]

    resp = await TeamService.add_team(user.user_id, _request("New Name", 2027))

    assert (resp.team_id, resp.already_exists) == (current.team_id, True)
    assert Team.select().where(Team.user_id == user.user_id).count() == 1


async def test_another_team_in_the_league_is_left_where_it_is(user, espn):
    """A leaguemate's team saved last season is still that team in this
    season's list, by its name: adding your own team does not take it over."""
    rival = _saved(user, "Bravo", 2026)
    espn["teams"][2027] = [{"id": 1, "name": "Alpha", "owners": [SWID]}, {"id": 2, "name": "Bravo", "owners": [OTHER]}]

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2027))

    assert resp.team_id != rival.team_id and resp.moved_from_season is None and resp.already_exists is False
    assert _info(rival.team_id)["year"] == 2026 and _info(rival.team_id)["team_name"] == "Bravo"


async def test_an_earlier_season_never_replaces_a_later_one(user, espn):
    current = _saved(user, "Alpha", 2027, espn_team_id=1)
    espn["teams"][2026] = [{"id": 1, "name": "Alpha", "owners": [SWID]}]

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2026))

    assert (resp.team_id, resp.already_exists, resp.moved_from_season) == (current.team_id, True, None)
    assert _info(current.team_id)["year"] == 2027


async def test_moving_keeps_the_scoring_preview_and_the_cookies_on_file(user, espn):
    last = _saved(user, "Alpha", 2026, scoring_preview="categories", espn_s2="AEB-ON-FILE", swid=SWID)
    espn["teams"][2027] = [{"id": 1, "name": "Alpha", "owners": [SWID]}]

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2027, espn_s2="", swid="", league_name=None))

    assert resp.moved_from_season == 2026
    info = _info(last.team_id)
    assert (info["scoring_preview"], info["espn_s2"], info["swid"], info["league_name"]) == (
        "categories", "AEB-ON-FILE", SWID, "The League")


async def test_an_update_to_this_season_follows_the_rename_in_the_identifier(user, espn):
    """The update route can move a team too (the Account desk's roll-over);
    its identifier follows the name ESPN answers with, so adding the team
    again finds it instead of saving a second copy."""
    last = _saved(user, "Old Name", 2026, espn_s2="AEB-ON-FILE", swid=SWID)
    espn["teams"][2027] = [{"id": 4, "name": "New Name", "owners": [SWID]}]

    await TeamService.update_team(user.user_id, last.team_id, _request("Old Name", 2027, espn_s2="", swid=""))

    team = Team.get_by_id(last.team_id)
    assert team.team_identifier == f"{LEAGUE}New Name"
    assert (_info(team.team_id)["year"], _info(team.team_id)["team_name"], _info(team.team_id)["espn_team_id"]) == (
        2027, "New Name", 4)
    again = await TeamService.add_team(user.user_id, _request("New Name", 2027))
    assert (again.team_id, again.already_exists) == (last.team_id, True)


async def test_a_leaguemate_who_took_the_old_name_is_not_your_team(user, espn):
    """Your 2026 team "Alpha" (ESPN id 1) is "Beta" now and a leaguemate's team
    is called "Alpha": adding that one does not move yours onto it."""
    mine = _saved(user, "Alpha", 2026, espn_team_id=1)
    espn["teams"][2027] = [{"id": 1, "name": "Beta", "owners": [SWID]}, {"id": 5, "name": "Alpha", "owners": [OTHER]}]

    resp = await TeamService.add_team(user.user_id, _request("Alpha", 2027))

    assert resp.team_id != mine.team_id and resp.moved_from_season is None
    assert (_info(mine.team_id)["year"], _info(mine.team_id)["espn_team_id"]) == (2026, 1)
    assert _info(resp.team_id)["espn_team_id"] == 5
