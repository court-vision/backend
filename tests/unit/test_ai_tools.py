"""
The AI layer's tool surface (services/ai/tools.py): what the model is allowed
to see, and how a failed lookup comes back to it.

Services are stubbed; the real ones are exercised by their own suites. What
matters here is the boundary: NBA IDs only, no game-log dump, and failures as
readable `is_error` results rather than exceptions that would sink the answer.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.errors import NotFoundError
from schemas.common import ApiStatus
from schemas.player import PlayerStatusData, PlayerStatusResp
from schemas.players_list import PlayerListItem, PlayersListData, PlayersListResp
from services.ai import tools
from services.player_service import PlayerService
from services.players_list_service import PlayersListService


def _run(name, raw):
    return asyncio.run(tools.run_tool(name, raw))


def _stats_resp(message="Player stats fetched successfully"):
    avg = SimpleNamespace(model_dump=lambda: {"avg_points": 21.4, "avg_fg_pct": 0.512})
    data = SimpleNamespace(
        id=1630578, name="Alperen Sengun", team="HOU", games_played=62, window="l10", window_games=10,
        avg_stats=avg, advanced_stats=None, game_logs=[{"pts": 30}] * 62,
    )
    return SimpleNamespace(data=data, message=message)


@pytest.mark.unit
class TestDefinitions:
    def test_every_definition_has_a_handler_and_every_handler_a_definition(self):
        defined = {t["name"] for t in [*tools.TOOLS, *tools.ROUTER_TOOLS, *tools.AGENT_TOOLS]}
        assert defined == set(tools._REGISTRY)

    def test_the_router_resolves_names_and_never_loads_stats(self):
        assert [t["name"] for t in tools.ROUTER_TOOLS] == ["search_players", "get_my_teams"]

    def test_schemas_forbid_extra_properties(self):
        for tool in [*tools.TOOLS, *tools.ROUTER_TOOLS, *tools.AGENT_TOOLS]:
            assert tool["input_schema"]["additionalProperties"] is False, tool["name"]

    def test_no_tool_takes_an_espn_id(self):
        for tool in [*tools.TOOLS, *tools.ROUTER_TOOLS, *tools.AGENT_TOOLS]:
            assert not any("espn" in prop for prop in tool["input_schema"]["properties"]), tool["name"]


@pytest.mark.unit
class TestSearchPlayers:
    def test_returns_nba_ids_and_never_espn_ids(self, monkeypatch):
        calls = []

        async def fake(**kwargs):
            calls.append(kwargs)
            return PlayersListResp(status=ApiStatus.SUCCESS, message="ok", data=PlayersListData(
                players=[PlayerListItem(id=1630578, espn_id=4871144, name="Alperen Sengun", team="HOU",
                                        position="C", games_played=62, avg_fpts=41.2, rank=18)],
                total=1, limit=8, offset=0, season="2025-26",
            ))
        monkeypatch.setattr(PlayersListService, "list_players", staticmethod(fake))

        outcome = _run("search_players", {"name": "Sengun"})

        assert not outcome.is_error
        body = json.loads(outcome.content)
        assert body["players"][0]["player_id"] == 1630578
        assert "espn_id" not in body["players"][0]
        assert calls == [{"name": "Sengun", "limit": 8}]

    def test_the_season_fallback_note_reaches_the_model(self, monkeypatch):
        """Before opening night the service serves last season's ranks and says so only in
        its message; without it the model would call a 2025-26 rank this season's."""
        async def fake(**kwargs):
            return PlayersListResp(
                status=ApiStatus.SUCCESS,
                message="Found 1 players (no 2026-27 data yet; showing 2025-26)",
                data=PlayersListData(
                    players=[PlayerListItem(id=1630578, name="Alperen Sengun", team="HOU", position="C",
                                            games_played=72, avg_fpts=43.8, rank=14)],
                    total=1, limit=8, offset=0, season="2025-26",
                ))
        monkeypatch.setattr(PlayersListService, "list_players", staticmethod(fake))

        body = json.loads(_run("search_players", {"name": "Sengun"}).content)

        assert body["season"] == "2025-26"
        assert "no 2026-27 data yet; showing 2025-26" in body["note"]

    def test_an_empty_list_is_a_result_not_an_error(self, monkeypatch):
        async def fake(**kwargs):
            return PlayersListResp(status=ApiStatus.SUCCESS, message="No player data available", data=None)
        monkeypatch.setattr(PlayersListService, "list_players", staticmethod(fake))

        outcome = _run("search_players", {"name": "Nobody"})

        assert not outcome.is_error
        assert json.loads(outcome.content)["players"] == []

    def test_a_failed_search_is_an_error_not_an_empty_result(self, monkeypatch):
        """Otherwise the model would tell the user the player doesn't exist."""
        async def fake(**kwargs):
            return PlayersListResp(status=ApiStatus.ERROR, message="Failed to fetch players", data=None)
        monkeypatch.setattr(PlayersListService, "list_players", staticmethod(fake))

        outcome = _run("search_players", {"name": "Sengun"})

        assert outcome.is_error
        assert outcome.content == "Failed to fetch players"


@pytest.mark.unit
class TestGetPlayerStats:
    def test_drops_the_game_log_and_keeps_the_season_note(self, monkeypatch):
        async def fake(**kwargs):
            assert kwargs == {"player_id": 1630578, "window": "l10"}
            return _stats_resp(message="No 2026-27 games yet; showing 2025-26")
        monkeypatch.setattr(PlayerService, "get_player_stats", staticmethod(fake))

        outcome = _run("get_player_stats", {"player_id": 1630578, "window": "l10"})

        body = json.loads(outcome.content)
        assert "game_logs" not in body
        assert body["per_game"] == {"avg_points": 21.4, "avg_fg_pct": 0.512}
        assert body["note"] == "No 2026-27 games yet; showing 2025-26"

    def test_window_defaults_to_season(self, monkeypatch):
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            return _stats_resp()
        monkeypatch.setattr(PlayerService, "get_player_stats", staticmethod(fake))

        _run("get_player_stats", {"player_id": 1630578})

        assert seen["window"] == "season"

    def test_a_service_error_is_readable_by_the_model(self, monkeypatch):
        async def fake(**kwargs):
            raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")
        monkeypatch.setattr(PlayerService, "get_player_stats", staticmethod(fake))

        outcome = _run("get_player_stats", {"player_id": 999})

        assert outcome.is_error
        assert outcome.content == "Player not found"


@pytest.mark.unit
class TestGetPlayerStatus:
    def test_no_current_report_is_null_not_healthy(self, monkeypatch):
        """The service answers None for last April's report in September, as for no report at all."""
        async def fake(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="No current injury report", data=None)
        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(fake))

        body = json.loads(_run("get_player_status", {"player_id": 1630578}).content)

        assert body == {"player_id": 1630578, "injury": None}

    def test_a_current_report_keeps_its_date_and_age(self, monkeypatch):
        async def fake(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="ok", data=PlayerStatusData(
                status="Out", injury_type="Ankle", injury_detail="Sprain",
                expected_return=None, report_date="2026-09-19", report_age_days=2))
        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(fake))

        body = json.loads(_run("get_player_status", {"player_id": 1630578}).content)

        assert body["injury"]["status"] == "Out"
        assert body["injury"]["report_date"] == "2026-09-19"
        assert body["injury"]["report_age_days"] == 2


@pytest.mark.unit
class TestDispatch:
    def test_unknown_tool(self):
        outcome = _run("drop_player", {"player_id": 1})
        assert outcome.is_error and "Unknown tool" in outcome.content

    @pytest.mark.parametrize("raw", [
        {},                                       # missing player_id
        {"player_id": 0},                         # not a real ID
        {"player_id": 1, "window": "last ten"},   # not season / lN
        {"player_id": 1, "espn_id": 4871144},     # extra property
        "1630578",                                # not an object
    ])
    def test_invalid_input_is_returned_not_raised(self, raw):
        outcome = _run("get_player_stats", raw)
        assert outcome.is_error and outcome.content.startswith("Invalid input")

    def test_an_unexpected_failure_hides_its_details(self, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("connection string postgres://secret@host")
        monkeypatch.setattr(PlayerService, "get_player_stats", staticmethod(boom))

        outcome = _run("get_player_stats", {"player_id": 1})

        assert outcome.is_error
        assert outcome.content == "The lookup failed"


def _team(team_id, name, league=None, league_name="Dorm League", scoring_preview="points"):
    from schemas.common import FantasyProvider
    return SimpleNamespace(
        team_id=team_id,
        league_info=SimpleNamespace(team_name=name, league_name=league_name, provider=FantasyProvider.ESPN,
                                    year=2027, scoring_preview=scoring_preview),
        league=league,
    )


@pytest.mark.unit
class TestGetMyTeams:
    def test_needs_a_caller(self):
        outcome = _run("get_my_teams", {})
        assert outcome.is_error

    def test_lists_the_callers_teams_and_only_the_fields_routing_needs(self, monkeypatch):
        from services.team_service import TeamService
        seen = []

        async def fake(user_id):
            seen.append(user_id)
            return SimpleNamespace(data=[
                _team(7, "Sengun Szn", league=SimpleNamespace(name="Dorm League 26-27", scoring_type="categories")),
                _team(9, "Punt FT%", league=None, league_name="Work League", scoring_preview="points"),
            ])
        monkeypatch.setattr(TeamService, "get_teams", staticmethod(fake))

        outcome = asyncio.run(tools.run_tool("get_my_teams", {}, ctx=tools.ToolContext(user_id=42)))

        assert seen == [42]
        teams = json.loads(outcome.content)["teams"]
        assert teams[0] == {"team_id": 7, "team_name": "Sengun Szn", "league_name": "Dorm League 26-27",
                            "provider": "espn", "season": 2027, "scoring": "categories"}
        assert (teams[1]["league_name"], teams[1]["scoring"]) == ("Work League", "points")

    def test_takes_no_arguments(self):
        outcome = asyncio.run(tools.run_tool("get_my_teams", {"user_id": 1}, ctx=tools.ToolContext(user_id=42)))
        assert outcome.is_error and outcome.content.startswith("Invalid input")


@pytest.mark.unit
class TestToolsets:
    def test_a_tool_outside_the_callers_toolset_is_unknown(self):
        """The router's model never sees the stats tools; if it named one anyway, it would not run."""
        outcome = asyncio.run(tools.run_tool("get_player_stats", {"player_id": 1}, allowed=tools.ROUTER_TOOL_NAMES))
        assert outcome.is_error and "Unknown tool" in outcome.content

    def test_ask_cannot_reach_the_team_lookup(self):
        outcome = asyncio.run(tools.run_tool("get_my_teams", {}, ctx=tools.ToolContext(user_id=42),
                                             allowed=tools.ASK_TOOL_NAMES))
        assert outcome.is_error and "Unknown tool" in outcome.content


# ---- the agent's team-scoped tools ----

def _agent(name, raw, user_id=42):
    return asyncio.run(tools.run_tool(name, raw, ctx=tools.ToolContext(user_id=user_id),
                                      allowed=tools.AGENT_TOOL_NAMES))


@pytest.fixture
def owns_22(monkeypatch):
    """User 42 owns team 22 and nothing else; returns the provider calls made."""
    provider_calls = []

    def owned(team_id, user_id):
        return SimpleNamespace(team_id=team_id, user_id=user_id) if (team_id, user_id) == (22, 42) else None

    async def league_info(team):
        provider_calls.append(("credentials", team.team_id))
        return SimpleNamespace(provider="espn")

    async def run_db(name, fn, *args):
        return fn(*args)
    monkeypatch.setattr(tools, "run_db", run_db)
    monkeypatch.setattr(tools, "ensure_team_owned", owned)
    monkeypatch.setattr(tools, "load_owned_league_info", league_info)
    return provider_calls


def _player(espn_id, nba_id, name, **kw):
    base = dict(player_id=espn_id, nba_player_id=nba_id, name=name, team="DET", position="C", lineup_slot="C",
                avg_points=1.8, projected_points=0.0, games_remaining=3, injured=False, injury_status=None)
    return SimpleNamespace(**{**base, **kw})


def _cat(key, you, opp, winner):
    return SimpleNamespace(key=key, label=key.upper(), you=you, opp=opp, winner=winner,
                           higher_is_better=key != "tov", is_rate=False)


@pytest.mark.unit
class TestAgentTeamTools:
    def test_someone_else_s_team_is_refused_before_any_provider_call(self, owns_22, monkeypatch):
        from services.matchup_service import MatchupService
        called = []

        async def matchup(*a, **k):
            called.append(a)
        monkeypatch.setattr(MatchupService, "get_matchup_by_team_id", staticmethod(matchup))

        outcome = _agent("get_matchup", {"team_id": 23})

        assert outcome.is_error and "not one of the user's teams" in outcome.content
        assert called == [] and owns_22 == []

    def test_the_matchup_carries_nba_ids_and_merges_the_category_projection(self, owns_22, monkeypatch):
        from services.matchup_service import MatchupService

        md = SimpleNamespace(
            matchup_period=1, matchup_period_start="2026-10-20", matchup_period_end="2026-10-26",
            scoring_format="categories", projected_margin=2.0,
            your_team=SimpleNamespace(team_name="Mine", current_score=0, projected_score=5,
                                      roster=[_player(4396993, 1631105, "Jalen Duren")]),
            opponent_team=SimpleNamespace(team_name="Theirs", current_score=0, projected_score=3,
                                          roster=[_player(1, None, "Unmapped", injured=True)]),
            category_comparison=SimpleNamespace(items=[_cat("blk", 0, 0, "tie")]),
            projected_category_comparison=SimpleNamespace(items=[_cat("blk", 19, 23, "opp"), _cat("tov", 30, 35, "you")]),
        )
        seen = []

        async def matchup(user_id, team_id, window):
            seen.append((user_id, team_id, window))
            return SimpleNamespace(data=md, message="ok")
        monkeypatch.setattr(MatchupService, "get_matchup_by_team_id", staticmethod(matchup))

        outcome = _agent("get_matchup", {"team_id": 22, "window": "last_14"})

        assert seen == [(42, 22, "last_14")]
        body = json.loads(outcome.content)
        assert body["your_roster"][0]["player_id"] == 1631105
        assert body["opponent_roster"][0] == {**body["opponent_roster"][0], "player_id": None, "injury": "injured"}
        assert "4396993" not in outcome.content
        blk, tov = body["categories"]
        assert (blk["you"], blk["projected_you"], blk["projected_opponent"], blk["projected_leader"]) == (0, 19, 23, "opp")
        assert (tov["you"], tov["projected_you"], tov["lower_is_better"]) == (None, 30, True)

    def test_free_agents_are_cut_to_the_limit_with_nba_ids(self, owns_22, monkeypatch):
        from services.streamer_service import StreamerService
        seen = []

        def fa(espn_id, nba_id):
            return SimpleNamespace(player_id=espn_id, nba_player_id=nba_id, name=f"P{nba_id}", team="UTA",
                                   valid_positions=["PF", "C"], avg_points_last_n=2.0, avg_points_season=1.5,
                                   games_remaining=4, game_days=[1, 2], b2b_game_count=1, streamer_score=9.0,
                                   injured=False, injury_status=None, acquisition_status=None, waivers_until=None)

        async def find(**kwargs):
            seen.append(kwargs)
            return SimpleNamespace(message="ok", data=SimpleNamespace(
                matchup_number=1, start_date="2026-10-20", end_date="2026-10-26", current_day_index=0,
                target_day=2, upcoming=True, value_kind="category_value",
                streamers=[fa(100 + i, 1_600_000 + i) for i in range(5)]))
        monkeypatch.setattr(StreamerService, "find_streamers", staticmethod(find))

        outcome = _agent("get_free_agents", {"team_id": 22, "mode": "day", "day": 2, "limit": 2})

        body = json.loads(outcome.content)
        assert [p["player_id"] for p in body["players"]] == [1_600_000, 1_600_001]
        assert body["pool_size"] == 5 and body["value_kind"] == "category_value"
        assert body["players"][0]["positions"] == "PF/C"
        assert seen[0]["target_day"] == 2 and seen[0]["mode"].value == "daily" and seen[0]["team_id"] == 22
        assert "101" not in json.dumps([p["player_id"] for p in body["players"]])

    def test_limits_are_validated(self, owns_22):
        assert _agent("get_free_agents", {"team_id": 22, "limit": 500}).is_error
        assert _agent("get_game_log", {"player_id": 1, "games": 50}).is_error
