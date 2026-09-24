"""
The model-visible tool surface. Each tool is a thin wrapper over an existing
service: no new logic, no aggregation invented here. The service computes;
the tool only chooses which fields the model gets to see.

Two rules shape every result:

- **NBA player IDs only.** Results expose `player_id` (nba.players.id) and
  never an ESPN ID, so the model has one ID system to get right.
- **Narrow, not dumps.** `get_player_stats` drops the full game log the stats
  endpoint returns: it is the bulk of that payload, and every byte of a tool
  result is billed as input on each later model call.

Tool definitions are a module-level constant in a fixed order. They render
ahead of the system prompt, so reordering them would change every request's
prefix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.errors import AppError
from core.logging import get_logger
from schemas.common import ApiStatus
from services.player_service import PlayerService
from services.players_list_service import PlayersListService


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchPlayersInput(_Input):
    name: str = Field(..., min_length=2, max_length=60)


class PlayerStatsInput(_Input):
    player_id: int = Field(..., gt=0)
    window: str = Field("season", pattern=r"^(season|l[1-9][0-9]?)$")


class PlayerStatusInput(_Input):
    player_id: int = Field(..., gt=0)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_players",
        "description": (
            "Find NBA players by name, or part of a name. Returns up to 8 matches, "
            "each with the NBA player_id every other tool needs, plus team, "
            "position, games played, and fantasy rank for the season named in "
            "`season` -- last season's until the new one has games, which `note` "
            "says when it happens."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full or partial player name, e.g. 'Sengun'"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_player_stats",
        "description": (
            "Per-game averages for one player over a window of recent games, plus "
            "advanced stats (usage, PIE, net rating, assist rate) when available. "
            "The window applies to per_game only; advanced stats are always "
            "season-level. Shooting percentages are computed from total makes and attempts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer", "description": "NBA player_id from search_players"},
                "window": {
                    "type": "string",
                    "description": "'season' (default), or 'lN' for the last N games, e.g. 'l10'",
                },
            },
            "required": ["player_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_player_status",
        "description": (
            "A player's current injury report: status, injury, expected return, "
            "report_date, and report_age_days (never more than 7). injury=null means "
            "Court Vision has no current report, which is not proof of health: say "
            "there is no current injury information rather than calling the player healthy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer", "description": "NBA player_id from search_players"},
            },
            "required": ["player_id"],
            "additionalProperties": False,
        },
    },
]


async def _search_players(args: SearchPlayersInput) -> dict[str, Any]:
    resp = await PlayersListService.list_players(name=args.name, limit=8)
    # The service reports its own failures as an ERROR envelope with no data.
    # Read as an empty search, that would have the model tell the user no such
    # player exists; raising makes it a tool error instead.
    if resp.status != ApiStatus.SUCCESS:
        raise AppError("PLAYER_SEARCH_FAILED", resp.message or "Player search failed")
    data = resp.data
    if data is None:
        return {"players": [], "total": 0, "note": resp.message}
    return {
        "players": [
            {
                "player_id": p.id,
                "name": p.name,
                "team": p.team,
                "position": p.position,
                "games_played": p.games_played,
                "avg_fpts": p.avg_fpts,
                "rank": p.rank,
            }
            for p in data.players
        ],
        "total": data.total,
        "season": data.season,
        # Carries the service's season note ("no 2026-27 data yet; showing ...")
        "note": resp.message,
    }


async def _get_player_stats(args: PlayerStatsInput) -> dict[str, Any]:
    resp = await PlayerService.get_player_stats(player_id=args.player_id, window=args.window)
    stats = resp.data
    return {
        "player_id": stats.id,
        "name": stats.name,
        "team": stats.team,
        "games_played": stats.games_played,
        "window": stats.window,
        "window_games": stats.window_games,
        "per_game": stats.avg_stats.model_dump(),
        "advanced": stats.advanced_stats.model_dump() if stats.advanced_stats else None,
        # Carries the service's season note ("no 2026-27 games yet; showing ...")
        "note": resp.message,
    }


async def _get_player_status(args: PlayerStatusInput) -> dict[str, Any]:
    resp = await PlayerService.get_player_status(args.player_id)
    # The service returns only a report from the last seven days, with its
    # age already on it; an older one comes back as no report at all.
    return {
        "player_id": args.player_id,
        "injury": resp.data.model_dump() if resp.data else None,
    }


@dataclass(frozen=True)
class _Tool:
    input_model: type[_Input]
    handler: Callable[[Any], Awaitable[dict[str, Any]]]


_REGISTRY: dict[str, _Tool] = {
    "search_players": _Tool(SearchPlayersInput, _search_players),
    "get_player_stats": _Tool(PlayerStatsInput, _get_player_stats),
    "get_player_status": _Tool(PlayerStatusInput, _get_player_status),
}
assert list(_REGISTRY) == [t["name"] for t in TOOLS], "TOOLS and _REGISTRY must list the same tools in order"


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool


async def run_tool(name: str, raw_input: Any) -> ToolOutcome:
    """Validate and run one tool call. Failures come back as `is_error` results for
    the model to read, never as exceptions: one bad lookup should not sink the answer."""
    tool = _REGISTRY.get(name)
    if tool is None:
        return ToolOutcome(f"Unknown tool: {name}", is_error=True)
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        return ToolOutcome(f"Invalid input: {problems}", is_error=True)
    try:
        result = await tool.handler(args)
    except AppError as exc:
        # Service-named failures (PLAYER_NOT_FOUND, ...) are meant to be read
        return ToolOutcome(exc.message, is_error=True)
    except Exception:
        get_logger().exception("ai_tool_failed", tool=name)
        return ToolOutcome("The lookup failed", is_error=True)
    return ToolOutcome(json.dumps(result, default=str), is_error=False)
