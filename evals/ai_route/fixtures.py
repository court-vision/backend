"""
The eval's fixed world: who is asking, which fantasy teams they own, and what
season it is. Everything else the router touches is real -- the prompt, the
tools, the loop, the validation, and player lookups against nba.players.

Four things are pinned because they would otherwise make yesterday's expected
answers wrong tomorrow:

- **The asker's teams.** A fixture user who owns two teams, one points league
  and one categories league, so "my other team" and "my 9-cat league" have a
  right answer that no real account's roster changes can move.
- **The season line.** The real one flips on opening night; cases say which
  side of it they are on.
- **Quota and the question log.** The eval is not a user: it neither spends a
  real account's daily allowance nor writes its questions to usr.ai_questions.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from services.ai import guards, questions, routing, service
from services.team_service import TeamService

# No real account: usr.users ids are nowhere near this, and it fits an int4
FIXTURE_USER_ID = 2_000_000_001

TEAMS = [
    {"team_id": 101, "team_name": "Area 51 Ballers", "league_name": "Sunday Hoopers", "provider": "espn",
     "season": 2027, "scoring": "points"},
    {"team_id": 102, "team_name": "Splash Cousins", "league_name": "Dorm 9-Cat", "provider": "yahoo",
     "season": 2027, "scoring": "categories"},
]
OWNED = frozenset(team["team_id"] for team in TEAMS)

# Byte-for-byte what services.ai.service._season_line_sync produces
SEASON_LINES = {
    "preseason": "NBA season: 2026-27 starts 2026-10-20; the latest season with games is 2025-26.",
    "in_progress": "NBA season: 2026-27, in progress.",
}

# Display only (the review page and the report's explanations). Never sent to the model.
NAMES: dict[int, str] = {
    101: "Area 51 Ballers (points)", 102: "Splash Cousins (categories)",
    1630578: "Alperen Sengun", 1627734: "Domantas Sabonis", 203999: "Nikola Jokić", 1629029: "Luka Dončić",
    1641705: "Victor Wembanyama", 1628983: "Shai Gilgeous-Alexander", 203507: "Giannis Antetokounmpo",
    201939: "Stephen Curry", 2544: "LeBron James", 1628369: "Jayson Tatum", 1630162: "Anthony Edwards",
    1630595: "Cade Cunningham", 1631114: "Jalen Williams", 1630552: "Jalen Johnson", 1641708: "Amen Thompson",
    1642843: "Cooper Flagg", 1642851: "Kon Knueppel", 203954: "Joel Embiid", 1626164: "Devin Booker",
    1628389: "Bam Adebayo", 1629627: "Zion Williamson", 1627759: "Jaylen Brown",
}


@dataclass
class Recorder:
    """What one (case, rep) saw: its model calls and the row the question log would have stored."""

    season: str = "preseason"
    calls: list[dict[str, Any]] = field(default_factory=list)
    question_row: Optional[dict[str, Any]] = None


_current: ContextVar[Optional[Recorder]] = ContextVar("ai_route_eval_recorder", default=None)


def recording(recorder: Recorder) -> Any:
    """Bind a recorder to the current task. Each case runs in its own task, so
    concurrent cases never see each other's calls."""
    return _current.set(recorder)


def _recorder() -> Recorder:
    recorder = _current.get()
    if recorder is None:
        raise RuntimeError("evals.ai_route.fixtures: no recorder bound to this task")
    return recorder


def install(patch: Callable[[Any, str, Any], None] = setattr) -> None:
    """Patch the six seams. The runner calls this once; a test passes
    `monkeypatch.setattr` as `patch` so the seams are restored afterwards."""

    async def no_quota(user_id: int) -> None:
        return None
    patch(guards, "consume_quota", no_quota)

    async def capture(**row: Any) -> Optional[int]:
        _recorder().question_row = row
        return None
    patch(questions, "record", capture)

    async def season_line() -> str:
        return SEASON_LINES[_recorder().season]
    patch(service, "_season_line", season_line)

    real_create = service._create

    async def spy_create(**kwargs: Any) -> Any:
        started = time.monotonic()
        call: dict[str, Any] = {"system": kwargs["system"], "messages": list(kwargs["messages"]),
                                "model": kwargs["model"], "response": None}
        _recorder().calls.append(call)
        response = await real_create(**kwargs)
        call["response"] = response
        call["latency_s"] = round(time.monotonic() - started, 3)
        return response
    patch(service, "_create", spy_create)

    real_get_teams = TeamService.get_teams

    async def get_teams(user_id: int) -> Any:
        if user_id != FIXTURE_USER_ID:
            return await real_get_teams(user_id)
        return SimpleNamespace(data=[
            SimpleNamespace(team_id=team["team_id"], league=None, league_info=SimpleNamespace(
                team_name=team["team_name"], league_name=team["league_name"], provider=team["provider"],
                year=team["season"], scoring_preview=team["scoring"]))
            for team in TEAMS
        ])
    patch(TeamService, "get_teams", staticmethod(get_teams))

    real_lookup = routing._lookup

    async def lookup(player_ids: list[int], team_ids: list[int], nba_teams: list[str], user_id: int) -> Any:
        if user_id != FIXTURE_USER_ID:
            return await real_lookup(player_ids, team_ids, nba_teams, user_id)
        # Players and NBA teams are checked for real; ownership is the fixture's
        found = await real_lookup(player_ids, [], nba_teams, user_id)
        return routing._Found(found.players, frozenset(team_ids) & OWNED, found.nba_teams, found.nba_team_names)
    patch(routing, "_lookup", lookup)
