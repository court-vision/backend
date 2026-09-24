"""
The AI layer's tool surface (services/ai/tools.py): what the model is allowed
to see, and how a failed lookup comes back to it.

Services are stubbed; the real ones are exercised by their own suites. What
matters here is the boundary: NBA IDs only, no game-log dump, and failures as
readable `is_error` results rather than exceptions that would sink the answer.
"""

import asyncio
import json
from datetime import date
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
    def test_every_definition_has_a_handler_in_the_same_order(self):
        assert [t["name"] for t in tools.TOOLS] == list(tools._REGISTRY)

    def test_schemas_forbid_extra_properties(self):
        for tool in tools.TOOLS:
            assert tool["input_schema"]["additionalProperties"] is False, tool["name"]

    def test_no_tool_takes_an_espn_id(self):
        for tool in tools.TOOLS:
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

    def test_an_empty_list_is_a_result_not_an_error(self, monkeypatch):
        async def fake(**kwargs):
            return PlayersListResp(status=ApiStatus.SUCCESS, message="No player data available", data=None)
        monkeypatch.setattr(PlayersListService, "list_players", staticmethod(fake))

        outcome = _run("search_players", {"name": "Nobody"})

        assert not outcome.is_error
        assert json.loads(outcome.content)["players"] == []


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
    @pytest.fixture(autouse=True)
    def today(self, monkeypatch):
        monkeypatch.setattr(tools, "nba_date_et", lambda: date(2026, 9, 21))

    @staticmethod
    def _report(monkeypatch, report_date):
        async def fake(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="ok", data=PlayerStatusData(
                status="Out", injury_type="Ankle", injury_detail="Sprain",
                expected_return=None, report_date=report_date))
        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(fake))

    def test_no_report_is_null_not_healthy(self, monkeypatch):
        async def fake(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="No injury record found", data=None)
        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(fake))

        body = json.loads(_run("get_player_status", {"player_id": 1630578}).content)

        assert body == {"player_id": 1630578, "injury": None, "report_age_days": None, "today": "2026-09-21"}

    def test_an_old_report_says_how_old_it_is(self, monkeypatch):
        """The real case that prompted this: in September the latest report is April's."""
        self._report(monkeypatch, "2026-04-12")

        body = json.loads(_run("get_player_status", {"player_id": 1630578}).content)

        assert body["injury"]["status"] == "Out"
        assert body["report_age_days"] == 162
        assert body["today"] == "2026-09-21"

    def test_an_unparseable_date_has_no_age(self, monkeypatch):
        self._report(monkeypatch, None)

        body = json.loads(_run("get_player_status", {"player_id": 1630578}).content)

        assert body["report_age_days"] is None


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
