"""
ESPN rosters / free agents / matchups carry `avg_points` computed from our stored
stats under the league's scoring; ESPN's own appliedAverage (or the default-formula
proxy over its raw averages) is only the last resort. The ESPN HTTP layer
(`provider_get`) and the value dispatcher are stubbed.
"""

import asyncio
import json
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

    # Matchup rosters batch-resolve ESPN IDs -> NBA IDs; `nba_ids` controls the map.
    state["nba_ids"] = {}
    monkeypatch.setattr(
        espn_service, "nba_ids_by_espn_id",
        lambda espn_ids: {eid: state["nba_ids"][eid] for eid in espn_ids if eid in state["nba_ids"]},
    )
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


# ---- pool status (free agent vs waivers) ---------------------------------------------


WAIVER_MS = 1761393600000   # 2025-10-25 12:00 UTC = 08:00 ET, an ESPN Sunday waiver run


@pytest.mark.unit
def test_free_agents_carry_their_pool_status(espn):
    """The pool entry IS the free-agent listing: `status` and `waiverProcessDate` sit next
    to `player` (2026-09-09 probe). WAIVERS -> a claim clearing on waivers_until; FREEAGENT
    -> an immediate pickup; anything else (ONTEAM, absent) -> None."""
    espn["payload"] = {"players": [
        {"id": 1, "status": "FREEAGENT", "onTeamId": 0, "rosterLocked": False, "player": _espn_player(1, "Free Guy", 10.0)},
        {"id": 2, "status": "WAIVERS", "onTeamId": 0, "waiverProcessDate": WAIVER_MS, "player": _espn_player(2, "Waiver Guy", 9.0)},
        {"id": 3, "status": "ONTEAM", "onTeamId": 4, "player": _espn_player(3, "Rostered Guy", 8.0)},
        {"id": 4, "player": _espn_player(4, "Bare Guy", 7.0)},
    ]}
    resp = asyncio.run(EspnService.get_free_agents(LEAGUE, 50))
    by_id = {p.player_id: p for p in resp.data}
    assert [(by_id[i].acquisition_status, by_id[i].waivers_until) for i in (1, 2, 3, 4)] == [
        ("free_agent", None), ("waivers", date(2025, 10, 25)), (None, None), (None, None)]


@pytest.mark.unit
def test_roster_entries_read_the_pool_status_under_player_pool_entry(espn):
    espn["payload"] = {"teams": [{"id": 1, "name": "My Team", "roster": {"entries": [
        {"lineupSlotId": 0, "status": "NORMAL",
         "playerPoolEntry": {"id": 1, "status": "ONTEAM", "onTeamId": 1, "rosterLocked": True,
                             "player": _espn_player(1, "Mine", 20.0)}},
    ]}}]}
    resp = asyncio.run(EspnService.get_team_data(LEAGUE))
    assert resp.data[0].acquisition_status is None and resp.data[0].waivers_until is None
    player = espn_service.Player({"lineupSlotId": 0, "status": "NORMAL",
                                  "playerPoolEntry": {"status": "ONTEAM", "onTeamId": 1, "rosterLocked": True,
                                                      "player": _espn_player(1, "Mine", 20.0)}}, YEAR)
    assert (player.poolStatus, player.onTeamId, player.rosterLocked) == ("ONTEAM", 1, True)   # not the entry's NORMAL


@pytest.mark.unit
def test_waiver_date_is_the_et_day_the_window_closes():
    assert espn_service.waiver_date(WAIVER_MS) == date(2025, 10, 25)
    assert espn_service.waiver_date(1761350400000) == date(2025, 10, 24)   # 2025-10-25 00:00 UTC is still the 24th in ET
    assert espn_service.waiver_date(None) is None and espn_service.waiver_date(0) is None
    assert espn_service.waiver_date("not-a-number") is None


@pytest.mark.unit
def test_player_pool_entries_are_a_targeted_filter_ids_read(monkeypatch):
    """One `filterIds` read keyed by id. ESPN refuses `limit` without a sort and, given
    `scoringPeriodId=0`, reports every entry as locked — so the header carries a sort
    and the params carry the board's real period (2026-09-09 probe)."""
    seen = {}

    async def provider_get(provider, url, *, params=None, headers=None, cookies=None, expect_key=None, **_):
        seen.update(provider=provider, url=url, params=params, headers=headers, expect_key=expect_key,
                    cookie_keys=sorted((cookies or {}).keys()))
        return {"players": [
            {"id": 6450, "status": "FREEAGENT", "onTeamId": 0, "rosterLocked": False, "lineupLocked": False,
             "player": {"id": 6450, "fullName": "Kawhi Leonard", "proTeamId": 12}},
            {"id": 3112335, "status": "ONTEAM", "onTeamId": 1, "rosterLocked": True,
             "player": {"id": 3112335, "fullName": "Nikola Jokic", "proTeamId": 7}},
            {"id": 77, "status": "WAIVERS", "onTeamId": 0, "waiverProcessDate": WAIVER_MS,
             "player": {"id": 77, "fullName": "Waiver Guy", "proTeamId": 2}},
            {"player": {"fullName": "No id"}},
        ]}

    monkeypatch.setattr(espn_service, "provider_get", provider_get)
    entries = asyncio.run(EspnService.get_player_pool_entries(LEAGUE, [3112335, 6450, 6450, 77], scoring_period_id=12))

    assert seen["provider"] == "espn" and seen["url"].endswith("/seasons/2027/segments/0/leagues/555")
    assert seen["params"] == {"view": "kona_player_info", "scoringPeriodId": 12}   # never 0
    assert seen["expect_key"] == "players" and seen["cookie_keys"] == ["SWID", "espn_s2"]
    assert json.loads(seen["headers"]["x-fantasy-filter"]) == {"players": {
        "filterIds": {"value": [77, 6450, 3112335]}, "limit": 3, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    assert set(entries) == {6450, 3112335, 77}
    assert entries[6450] == espn_service.PoolEntry(6450, "FREEAGENT", 0, False, "Kawhi Leonard", "LAC")
    assert entries[3112335] == espn_service.PoolEntry(3112335, "ONTEAM", 1, True, "Nikola Jokic", "DEN")
    assert entries[77].status == "WAIVERS" and entries[77].waivers_until == date(2025, 10, 25)

    seen.clear()
    assert asyncio.run(EspnService.get_player_pool_entries(LEAGUE, [])) == {} and seen == {}   # nothing to ask
    asyncio.run(EspnService.get_player_pool_entries(LEAGUE, [1]))
    assert seen["params"] == {"view": "kona_player_info"}                                     # no period: omitted


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


@pytest.mark.unit
def test_matchup_rosters_carry_resolved_nba_player_ids(matchup):
    """Terminal panels navigate by NBA ID, so matchup rows must carry one.

    Players the lookup does not know stay None rather than falling back to the
    ESPN ID, which would point at an unrelated player.
    """
    matchup["nba_ids"] = {1: 2544, 2: 203999, 4: 201142}

    resp = asyncio.run(EspnService.get_matchup_data(LEAGUE, scoring=POINTS))

    assert resp.status == ApiStatus.SUCCESS, resp.message
    ours = {p.player_id: p.nba_player_id for p in resp.data.your_team.roster}
    assert ours == {1: 2544, 2: 203999, 3: None}
    assert resp.data.opponent_team.roster[0].nba_player_id == 201142
