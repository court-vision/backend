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
        defined = {t["name"] for t in tools.TOOLS} | {t["name"] for t in tools.ROUTER_TOOLS}
        assert defined == set(tools._REGISTRY)

    def test_the_router_resolves_names_and_never_loads_stats(self):
        assert [t["name"] for t in tools.ROUTER_TOOLS] == ["search_players", "get_my_teams"]

    def test_schemas_forbid_extra_properties(self):
        for tool in [*tools.TOOLS, *tools.ROUTER_TOOLS]:
            assert tool["input_schema"]["additionalProperties"] is False, tool["name"]

    def test_no_tool_takes_an_espn_id(self):
        for tool in [*tools.TOOLS, *tools.ROUTER_TOOLS]:
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
