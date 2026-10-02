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

Tool definitions are module-level constants in a fixed order. They render
ahead of the system prompt, so reordering them would change every request's
prefix. Each endpoint gets its own toolset: `/ai/ask` answers from stats
(`TOOLS`), the router only resolves names (`ROUTER_TOOLS`), and `run_tool`
refuses anything outside the set the caller passes as `allowed`.
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
from services.team_service import TeamService
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


class MyTeamsInput(_Input):
    pass


@dataclass(frozen=True)
class ToolContext:
    """Who is asking. Team-scoped tools answer for this user and no one else."""

    user_id: int


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


async def _search_players(args: SearchPlayersInput, ctx: ToolContext | None) -> dict[str, Any]:
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


async def _get_player_stats(args: PlayerStatsInput, ctx: ToolContext | None) -> dict[str, Any]:
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


async def _get_player_status(args: PlayerStatusInput, ctx: ToolContext | None) -> dict[str, Any]:
    resp = await PlayerService.get_player_status(args.player_id)
    # The service returns only a report from the last seven days, with its
    # age already on it; an older one comes back as no report at all.
    return {
        "player_id": args.player_id,
        "injury": resp.data.model_dump() if resp.data else None,
    }


async def _get_my_teams(args: MyTeamsInput, ctx: ToolContext | None) -> dict[str, Any]:
    if ctx is None:
        raise AppError("AI_NO_CALLER", "This lookup needs a signed-in user")
    resp = await TeamService.get_teams(ctx.user_id)
    return {
        "teams": [
            {
                "team_id": team.team_id,
                "team_name": team.league_info.team_name,
                "league_name": (team.league.name if team.league and team.league.name
                                else team.league_info.league_name),
                "provider": getattr(team.league_info.provider, "value", team.league_info.provider),
                "season": team.league_info.year,
                "scoring": (team.league.scoring_type if team.league
                            else team.league_info.scoring_preview),
            }
            for team in (resp.data or [])
        ],
    }


GET_MY_TEAMS: dict[str, Any] = {
    "name": "get_my_teams",
    "description": (
        "The asking user's own fantasy teams: team_id, team name, league name, provider, "
        "season and scoring format. Use it when the user names or describes one of their "
        "teams or leagues, or asks for a team other than the one selected in the view. A "
        "team_id you put in an answer must come from this list or from the view."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

# The router resolves names; it never loads stats (docs/AI_PHASE1_PLAN.md § 4)
ROUTER_TOOLS: list[dict[str, Any]] = [TOOLS[0], GET_MY_TEAMS]
ASK_TOOL_NAMES = frozenset(t["name"] for t in TOOLS)
ROUTER_TOOL_NAMES = frozenset(t["name"] for t in ROUTER_TOOLS)


@dataclass(frozen=True)
class _Tool:
    input_model: type[_Input]
    handler: Callable[[Any, ToolContext | None], Awaitable[dict[str, Any]]]


_REGISTRY: dict[str, _Tool] = {
    "search_players": _Tool(SearchPlayersInput, _search_players),
    "get_player_stats": _Tool(PlayerStatsInput, _get_player_stats),
    "get_player_status": _Tool(PlayerStatusInput, _get_player_status),
    "get_my_teams": _Tool(MyTeamsInput, _get_my_teams),
}
assert ASK_TOOL_NAMES | ROUTER_TOOL_NAMES == set(_REGISTRY), "every defined tool needs a handler, and vice versa"


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool


async def run_tool(
    name: str,
    raw_input: Any,
    *,
    ctx: ToolContext | None = None,
    allowed: frozenset[str] | None = None,
) -> ToolOutcome:
    """Validate and run one tool call. Failures come back as `is_error` results for
    the model to read, never as exceptions: one bad lookup should not sink the answer.

    `allowed` is the calling endpoint's toolset; a name outside it is unknown even
    if another endpoint defines it."""
    tool = _REGISTRY.get(name) if allowed is None or name in allowed else None
    if tool is None:
        return ToolOutcome(f"Unknown tool: {name}", is_error=True)
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        return ToolOutcome(f"Invalid input: {problems}", is_error=True)
    try:
        result = await tool.handler(args, ctx)
    except AppError as exc:
        # Service-named failures (PLAYER_NOT_FOUND, ...) are meant to be read
        return ToolOutcome(exc.message, is_error=True)
    except Exception:
        get_logger().exception("ai_tool_failed", tool=name)
        return ToolOutcome("The lookup failed", is_error=True)
    return ToolOutcome(json.dumps(result, default=str), is_error=False)
