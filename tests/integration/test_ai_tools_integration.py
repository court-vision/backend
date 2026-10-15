"""
Integration: the AI agent's lookups that read the database themselves
(services/ai/tools.py), against the real schema.

The season game log (latest season by default, sorted by any stat, regular
season only unless asked, sub-minute appearances left out), the schedule's
game counts, the latest projection snapshot, draft rooms scoped to their
owner, and the answer comment (migration 0034).
"""

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from core.settings import settings
from db.models.ai_conversations import AiTurn
from db.models.drafts import DraftSession
from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from db.models.users import User
from services.ai import conversations, tools

pytestmark = [pytest.mark.integration]

BASE = {"fpts": 20, "pts": 10, "reb": 5, "ast": 2, "stl": 1, "blk": 1, "tov": 1,
        "min": 25, "fgm": 4, "fga": 9, "fg3m": 1, "fg3a": 3, "ftm": 1, "fta": 2}


def run(name, raw, user_id=1):
    outcome = asyncio.run(tools.run_tool(name, raw, ctx=tools.ToolContext(user_id=user_id),
                                         allowed=tools.AGENT_TOOL_NAMES))
    assert not outcome.is_error, outcome.content
    return json.loads(outcome.content)


@pytest.fixture
def league(integration_db):
    for abbrev in ("DET", "UTA", "BOS"):
        NBATeam.get_or_create(id=abbrev, defaults={"name": abbrev, "city": abbrev,
                                                   "conference": "East", "division": "Central"})
    Player.create(id=301, name="Big Man", name_normalized="big man")
    games = [
        # (game_id, date, season, home, away, blk, min)
        ("0022400001", date(2025, 3, 1), "2024-25", "DET", "BOS", 6, 30),
        ("0022500001", date(2025, 11, 1), "2025-26", "DET", "UTA", 2, 30),
        ("0022500002", date(2025, 11, 3), "2025-26", "UTA", "DET", 5, 31),
        ("0022500003", date(2025, 11, 5), "2025-26", "DET", "BOS", 3, 29),
        ("0022500004", date(2025, 11, 7), "2025-26", "BOS", "DET", 9, 0),    # sub-minute: left out
        ("0042500001", date(2026, 4, 20), "2025-26", "DET", "BOS", 7, 35),   # playoffs
    ]
    for gid, day, season, home, away, blk, minutes in games:
        stype = "playoffs" if gid.startswith("004") else "regular"
        Game.create(game_id=gid, game_date=day, season=season, home_team=home, away_team=away,
                    status="final", season_type=stype)
        PlayerGameStats.create(player=301, team="DET", game_date=day, game=gid, season=season,
                               season_type=stype, **{**BASE, "blk": blk, "min": minutes})
    return None


class TestGameLog:
    def test_latest_regular_season_newest_first_without_sub_minute_games(self, league):
        body = run("get_game_log", {"player_id": 301, "games": 10})
        assert body["season"] == "2025-26"
        assert [g["date"] for g in body["games"]] == ["2025-11-05", "2025-11-03", "2025-11-01"]
        assert body["games_played"] == 3
        assert body["games"][1]["opponent"] == "UTA" and body["games"][1]["home"] is False

    def test_best_games_by_a_stat_in_a_chosen_season(self, league):
        body = run("get_game_log", {"player_id": 301, "season": "2025-26", "sort_by": "blk", "games": 2})
        assert [g["blk"] for g in body["games"]] == [5, 3]
        assert run("get_game_log", {"player_id": 301, "season": "2024-25"})["games"][0]["blk"] == 6

    def test_playoffs_only_when_asked(self, league):
        body = run("get_game_log", {"player_id": 301, "season_type": "playoffs"})
        assert [g["blk"] for g in body["games"]] == [7]
        both = run("get_game_log", {"player_id": 301, "season_type": "all", "sort_by": "blk", "games": 1})
        assert both["games"][0]["blk"] == 7


class TestSchedule:
    def test_counts_per_team_and_per_day(self, league):
        body = run("get_schedule", {"teams": ["det", "UTA"], "start_date": "2025-11-01", "end_date": "2025-11-07"})
        det, uta = body["teams"]
        assert (det["team"], det["games"], det["dates"]) == ("DET", 4, "11-01, 11-03, 11-05, 11-07")
        assert (uta["team"], uta["games"]) == ("UTA", 2)
        assert body["games_per_day"][0] == {"date": "2025-11-01", "games": 1}

    def test_a_range_too_long_is_refused(self, league):
        outcome = asyncio.run(tools.run_tool("get_schedule", {"start_date": "2025-11-01", "end_date": "2026-01-01"},
                                             ctx=tools.ToolContext(user_id=1), allowed=tools.AGENT_TOOL_NAMES))
        assert outcome.is_error and "1-28 days" in outcome.content


class TestProjections:
    def test_the_latest_snapshot_sorted_by_a_stat(self, league, monkeypatch):
        monkeypatch.setattr(settings, "nba_season", "2026-27")
        Player.create(id=302, name="Rim Guard", name_normalized="rim guard")
        for as_of, blk_a, blk_b in ((date(2026, 10, 1), 9.0, 1.0), (date(2026, 10, 10), 2.1, 1.4)):
            PlayerProjection.create(player=301, season="2026-27", as_of_date=as_of, blk=blk_a, fgm=5, fga=10, projected_gp=70)
            PlayerProjection.create(player=302, season="2026-27", as_of_date=as_of, blk=blk_b, fgm=3, fga=0)
        body = run("get_projections", {"sort_by": "blk", "limit": 5})
        assert body["as_of"] == "2026-10-10"
        assert [(p["name"], p["blk"]) for p in body["players"]] == [("Big Man", 2.1), ("Rim Guard", 1.4)]
        assert body["players"][0]["fg_pct"] == 0.5 and body["players"][1]["fg_pct"] is None
        assert [p["player_id"] for p in run("get_projections", {"player_ids": [302]})["players"]] == [302]


class TestDraftRoomsAndComments:
    def _user(self, n):
        return User.create(email=f"tools{n}@courtvision.dev", clerk_user_id=f"user_tools_{n}",
                           created_at=datetime.now(timezone.utc))

    def test_rooms_are_the_caller_s_own(self, integration_db):
        me, other = self._user(1), self._user(2)
        mine = DraftSession.create(user=me.user_id, name="Home league")
        theirs = DraftSession.create(user=other.user_id, name="Not yours")

        rooms = run("get_draft_rooms", {}, user_id=me.user_id)["rooms"]
        assert [r["room_id"] for r in rooms] == [mine.id]
        assert run("get_draft_room", {"room_id": mine.id}, user_id=me.user_id)["name"] == "Home league"
        refused = asyncio.run(tools.run_tool("get_draft_room", {"room_id": theirs.id},
                                             ctx=tools.ToolContext(user_id=me.user_id), allowed=tools.AGENT_TOOL_NAMES))
        assert refused.is_error and "not one of the user's rooms" in refused.content

    def test_a_comment_is_the_caller_s_own_and_blank_clears_it(self, integration_db):
        me, other = self._user(1), self._user(2)
        conv = asyncio.run(conversations.create(me.user_id, None))
        turn_id = asyncio.run(conversations.record(conv.id, {"user_id": me.user_id, "message": "q", "outcome": "ok"}))

        assert asyncio.run(conversations.set_comment(turn_id, other.user_id, "not mine")) is False
        assert asyncio.run(conversations.set_comment(turn_id, me.user_id, "Wrong player")) is True
        row = AiTurn.get_by_id(turn_id)
        assert row.comment == "Wrong player" and row.comment_at is not None
        asyncio.run(conversations.set_comment(turn_id, me.user_id, None))
        row = AiTurn.get_by_id(turn_id)
        assert row.comment is None and row.comment_at is None
