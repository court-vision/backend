"""
ESPN rosters / free agents / matchups carry `avg_points` computed from our stored
stats under the league's scoring; ESPN's own appliedAverage (or the default-formula
proxy over its raw averages) is only the last resort. The ESPN HTTP layer
(`provider_get`) and the value dispatcher are stubbed.
"""

import asyncio
from datetime import date

import pytest

from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from services import espn_service
from services.espn_service import EspnService
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.points import DEFAULT_POINTS
from services.scoring.providers.espn_settings import statline_from_espn_stats
from services.scoring.resolver import ResolvedScoring, resolve_scoring

YEAR = 2027
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=555, team_name="My Team", year=YEAR,
                    espn_s2="s2", swid="{swid}")
CATEGORY = resolve_scoring(None, preview="categories")
POINTS = resolve_scoring(None)


def _espn_player(pid: int, name: str, applied_avg: float, avg_raw: dict | None = None, pro_team_id: int = 7) -> dict:
    split = {"seasonId": YEAR, "id": f"00{YEAR}", "scoringPeriodId": 0,
             "appliedTotal": applied_avg * 10, "appliedAverage": applied_avg}
    if avg_raw is not None:
        split["stats"] = {k: v * 10 for k, v in avg_raw.items()}
        split["averageStats"] = avg_raw
    # `id` precedes `stats`: the ESPN helper takes the first `id` it finds while recursing
    return {"id": pid, "fullName": name, "defaultPositionId": 1, "eligibleSlots": [0, 5],
            "proTeamId": pro_team_id, "injuryStatus": "ACTIVE", "injured": False, "stats": [split]}


def _roster_entry(player: dict, slot: int = 0) -> dict:
    return {"lineupSlotId": slot, "playerPoolEntry": {"player": player}}


RAW_AVG = {"0": 20.0, "6": 8.0, "3": 4.0, "13": 8.0, "14": 16.0}   # pts, reb, ast, fgm, fga
ROSTER_PAYLOAD = {"teams": [
    {"id": 1, "name": "My Team", "roster": {"entries": [
        _roster_entry(_espn_player(1, "Nikola Jokić", 30.0, RAW_AVG)),
        _roster_entry(_espn_player(2, "Role Player", 12.5)),
        _roster_entry(_espn_player(3, "Category League Guy", 0.0, RAW_AVG)),   # appliedAverage 0 in cat leagues
    ]}},
    {"id": 2, "name": "Other", "roster": {"entries": []}},
]}
FA_PAYLOAD = {"players": [
    {"id": 1, "player": _espn_player(1, "Nikola Jokić", 30.0, RAW_AVG)},
    {"id": 3, "player": _espn_player(3, "Category League Guy", 0.0, RAW_AVG)},
]}


@pytest.fixture
def espn(monkeypatch):
    """Stub ESPN HTTP + scoring resolution; `dispatch` controls what our value service returns."""
    state = {"payload": None, "scoring": POINTS, "values": {}, "calls": []}

    async def provider_get(*args, **kwargs):
        return state["payload"]

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(espn_service, "provider_get", provider_get)
    monkeypatch.setattr(espn_service, "run_db", direct_run_db)
    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(lambda li, team_id=None: state["scoring"]))

    def fake_avg_points_for(scoring, *, espn_ids=None, names=None, days=14, recent=False):
        state["calls"].append((scoring, list(espn_ids or []), days, recent))
        if isinstance(state["values"], Exception):
            raise state["values"]
        return {eid: state["values"].get(eid, ValueResult(None, None)) for eid in espn_ids}

    monkeypatch.setattr(PlayerValueService, "avg_points_for", staticmethod(fake_avg_points_for))
    return state


@pytest.mark.unit
def test_roster_avg_points_come_from_the_dispatcher_with_espn_as_last_resort(espn):
    espn["payload"] = ROSTER_PAYLOAD
    espn["scoring"] = CATEGORY
    espn["values"] = {1: ValueResult(61.5, "rolling"), 3: ValueResult(27.0, "baseline")}

    resp = asyncio.run(EspnService.get_team_data(LEAGUE))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert espn["calls"] == [(CATEGORY, [1, 2, 3], 14, False)]
    by_id = {p.player_id: p for p in resp.data}
    assert by_id[1].avg_points == 61.5 and by_id[1].value_source == "rolling"
    assert by_id[3].avg_points == 27.0 and by_id[3].value_source == "baseline"
    assert by_id[2].avg_points == 12.5 and by_id[2].value_source == "provider"     # we know nothing: ESPN's number
    assert all(p.value_kind == "cat_value" for p in resp.data)
    assert by_id[1].team == "DEN" and by_id[1].valid_positions == ["PG", "G", "UT1", "UT2", "UT3"]


@pytest.mark.unit
def test_free_agents_keep_the_default_formula_proxy_only_when_we_have_nothing(espn):
    espn["payload"] = FA_PAYLOAD
    espn["scoring"] = CATEGORY
    espn["values"] = {1: ValueResult(61.5, "rolling")}

    resp = asyncio.run(EspnService.get_free_agents(LEAGUE, 50))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert espn["calls"][0][1] == [1, 3]
    by_id = {p.player_id: p for p in resp.data}
    assert by_id[1].avg_points == 61.5 and by_id[1].value_source == "rolling"
    # ESPN reports appliedAverage=0 for category leagues; with no stored value the
    # pre-existing proxy (default points formula over ESPN's raw averages) survives
    assert by_id[3].avg_points == round(DEFAULT_POINTS.score(statline_from_espn_stats(RAW_AVG)), 2) > 0
    assert by_id[3].value_source == "provider" and by_id[3].value_kind == "cat_value"


@pytest.mark.unit
def test_points_league_values_are_fpts_and_lookup_failures_fall_back_to_espn(espn):
    espn["payload"] = ROSTER_PAYLOAD
    espn["scoring"] = POINTS
    espn["values"] = {1: ValueResult(58.0, "rolling"), 2: ValueResult(11.0, "baseline")}

    resp = asyncio.run(EspnService.get_team_data(LEAGUE))
    by_id = {p.player_id: p for p in resp.data}
    assert (by_id[1].avg_points, by_id[2].avg_points) == (58.0, 11.0)
    assert all(p.value_kind == "fpts" for p in resp.data)

    espn["values"] = RuntimeError("database unavailable")
    resp = asyncio.run(EspnService.get_team_data(LEAGUE))
    assert resp.status == ApiStatus.SUCCESS                                       # ESPN data is still served
    by_id = {p.player_id: p for p in resp.data}
    assert by_id[1].avg_points == 30.0 and by_id[2].avg_points == 12.5
    assert all(p.value_source == "provider" for p in resp.data)


@pytest.mark.unit
def test_scoring_preview_survives_a_failed_league_lookup(espn, monkeypatch):
    def boom(li, team_id=None):
        raise RuntimeError("no database")

    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(boom))
    espn["payload"] = ROSTER_PAYLOAD
    espn["values"] = {1: ValueResult(61.5, "rolling")}

    preview = LEAGUE.model_copy(update={"scoring_preview": "categories"})
    resp = asyncio.run(EspnService.get_team_data(preview))

    assert resp.status == ApiStatus.SUCCESS
    scoring = espn["calls"][0][0]
    assert scoring.is_categories and resp.data[0].value_kind == "cat_value"
    assert asyncio.run(EspnService.get_team_data(LEAGUE)).data[0].value_kind == "fpts"


# ---- matchup ------------------------------------------------------------------------


MATCHUP_PAYLOAD = {
    "status": {"currentMatchupPeriod": 1, "latestScoringPeriod": 1},
    "settings": {"scheduleSettings": {"matchupPeriods": {"1": [1]}}},
    "teams": [
        {"id": 1, "name": "My Team", "roster": {"entries": [
            _roster_entry(_espn_player(1, "Nikola Jokić", 30.0, RAW_AVG)),
            _roster_entry(_espn_player(2, "Role Player", 12.5, {"0": 10.0})),
            _roster_entry(_espn_player(3, "Opening Night Guy", 0.0)),                # no ESPN average yet
        ]}},
        {"id": 2, "name": "Opp", "roster": {"entries": [_roster_entry(_espn_player(4, "Opp Guy", 20.0))]}},
    ],
    "schedule": [{"matchupPeriodId": 1, "home": {"teamId": 1, "totalPoints": 100.0},
                  "away": {"teamId": 2, "totalPoints": 90.0}}],
}


@pytest.fixture
def matchup(espn, monkeypatch):
    espn["payload"] = MATCHUP_PAYLOAD
    monkeypatch.setattr(espn_service, "get_espn_matchup_dates", lambda *a, **k: (date(2026, 10, 20), date(2026, 10, 26)))
    monkeypatch.setattr(espn_service, "get_current_matchup", lambda *a, **k: {"matchup_number": 1})
    monkeypatch.setattr(espn_service, "get_remaining_games", lambda *a, **k: 3)
    return espn


@pytest.mark.unit
def test_category_matchup_player_values_are_category_values(matchup):
    matchup["values"] = {1: ValueResult(80.0, "baseline"), 3: ValueResult(18.0, "baseline"), 4: ValueResult(40.0, "baseline")}

    resp = asyncio.run(EspnService.get_matchup_data(LEAGUE, scoring=CATEGORY))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    assert matchup["calls"][0][1] == [1, 2, 3, 4]                                # both rosters, one lookup
    ours = {p.player_id: p.avg_points for p in resp.data.your_team.roster}
    assert ours == {1: 80.0, 2: 12.5, 3: 18.0}                                    # ours wins; ESPN only for the unknown
    assert resp.data.opponent_team.roster[0].avg_points == 40.0
    assert resp.data.scoring_format == "categories"


@pytest.mark.unit
def test_points_matchup_keeps_espn_window_average_and_fills_opening_week_zeros(matchup):
    matchup["values"] = {1: ValueResult(58.0, "rolling"), 3: ValueResult(18.0, "baseline")}

    resp = asyncio.run(EspnService.get_matchup_data(LEAGUE, scoring=POINTS))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    ours = {p.player_id: p.avg_points for p in resp.data.your_team.roster}
    assert ours == {1: 30.0, 2: 12.5, 3: 18.0}                                    # ESPN's window average when it has one
    assert resp.data.your_team.projected_score == round(100.0 + (30.0 + 12.5 + 18.0) * 3, 2)
    assert resp.data.opponent_team.projected_score == round(90.0 + 20.0 * 3, 2)
