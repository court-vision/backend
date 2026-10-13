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
(`TOOLS`), the router only resolves names (`ROUTER_TOOLS`), the agent reads
the user's teams as well (`AGENT_TOOLS`), and `run_tool` refuses anything
outside the set the caller passes as `allowed`.

Team-scoped tools check that the caller owns the team before any provider
call: a team_id the model made up is "not one of your teams", never another
user's league.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.deps import OwnedTeamContext, ensure_team_owned, load_owned_league_info
from core.errors import AppError
from core.logging import get_logger
from db.base import run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.streamer import StreamerMode
from services.matchup_service import MatchupService
from services.player_games_service import PlayerGamesService
from services.player_service import PlayerService
from services.scheduled_pickup_service import ScheduledPickupService
from services.streamer_service import StreamerService
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


class TeamInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)


class MatchupInput(TeamInput):
    window: Literal["season", "last_7", "last_14", "last_30"] = "season"


class FreeAgentsInput(TeamInput):
    mode: Literal["week", "day"] = "week"
    day: Optional[int] = Field(None, ge=0, le=13)
    limit: int = Field(15, ge=1, le=30)
    include_injured: bool = False
    b2b_only: bool = False


class GameLogInput(_Input):
    player_id: int = Field(..., gt=0, lt=2**31)
    games: int = Field(5, ge=1, le=15)


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


async def list_my_teams(user_id: int) -> list[dict[str, Any]]:
    """One user's fantasy teams, as the router knows them. The tool returns all of
    it; the router's view carries everything but the names (`routing.describe_view`)."""
    resp = await TeamService.get_teams(user_id)
    return [
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
    ]


async def _get_my_teams(args: MyTeamsInput, ctx: ToolContext | None) -> dict[str, Any]:
    if ctx is None:
        raise AppError("AI_NO_CALLER", "This lookup needs a signed-in user")
    return {"teams": await list_my_teams(ctx.user_id)}


GET_MY_TEAMS: dict[str, Any] = {
    "name": "get_my_teams",
    "description": (
        "The asking user's own fantasy teams: team_id, team name, league name, provider, "
        "season and scoring format. The view already lists each team's ID, scoring format "
        "and provider, so use this only when the user calls a team or league by its name, "
        "or the view lists no teams. A team_id you put in an answer must come from this "
        "list or from the view."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

async def _owned(ctx: ToolContext | None, team_id: int) -> tuple[OwnedTeamContext, LeagueInfo]:
    if ctx is None:
        raise AppError("AI_NO_CALLER", "This lookup needs a signed-in user")
    team = await run_db("ai.owned_team", ensure_team_owned, team_id, ctx.user_id)
    if team is None:
        raise AppError("TEAM_NOT_FOUND", f"Team {team_id} is not one of the user's teams")
    return team, await load_owned_league_info(team)


def _injury(injured: bool, status: Optional[str]) -> Optional[str]:
    return status or ("injured" if injured else None)


def _roster_row(p: Any) -> dict[str, Any]:
    return {
        "player_id": p.nba_player_id,
        "name": p.name,
        "team": p.team,
        "position": p.position,
        "slot": p.lineup_slot,
        "avg_value": p.avg_points,
        "games_left": p.games_remaining,
        "injury": _injury(p.injured, p.injury_status),
    }


def _categories(md: Any) -> Optional[list[dict[str, Any]]]:
    now = {c.key: c for c in (md.category_comparison.items if md.category_comparison else [])}
    projected = {c.key: c for c in (md.projected_category_comparison.items if md.projected_category_comparison else [])}
    if not now and not projected:
        return None
    rows = []
    for key in [*now, *(k for k in projected if k not in now)]:
        c, p = now.get(key), projected.get(key)
        ref = c or p
        rows.append({
            "category": key,
            "label": ref.label,
            "you": c.you if c else None,
            "opponent": c.opp if c else None,
            "leader": c.winner if c else None,
            "projected_you": p.you if p else None,
            "projected_opponent": p.opp if p else None,
            "projected_leader": p.winner if p else None,
            "is_rate": ref.is_rate,
            "lower_is_better": not ref.higher_is_better,
        })
    return rows


async def _get_matchup(args: MatchupInput, ctx: ToolContext | None) -> dict[str, Any]:
    await _owned(ctx, args.team_id)
    resp = await MatchupService.get_matchup_by_team_id(ctx.user_id, args.team_id, args.window)
    md = resp.data
    if md is None:
        return {"team_id": args.team_id, "matchup": None, "note": resp.message or "No current matchup"}
    return {
        "team_id": args.team_id,
        "matchup_period": md.matchup_period,
        "start": md.matchup_period_start,
        "end": md.matchup_period_end,
        "scoring": md.scoring_format,
        "window": args.window,
        "you": {"team_name": md.your_team.team_name, "score": md.your_team.current_score,
                "projected": md.your_team.projected_score},
        "opponent": {"team_name": md.opponent_team.team_name, "score": md.opponent_team.current_score,
                     "projected": md.opponent_team.projected_score},
        "projected_margin": md.projected_margin,
        "categories": _categories(md),
        "your_roster": [_roster_row(p) for p in md.your_team.roster],
        "opponent_roster": [_roster_row(p) for p in md.opponent_team.roster],
    }


async def _get_free_agents(args: FreeAgentsInput, ctx: ToolContext | None) -> dict[str, Any]:
    _, league_info = await _owned(ctx, args.team_id)
    resp = await StreamerService.find_streamers(
        league_info=league_info,
        fa_count=150,
        exclude_injured=not args.include_injured,
        b2b_only=args.b2b_only,
        mode=StreamerMode.DAILY if args.mode == "day" else StreamerMode.WEEK,
        target_day=args.day,
        avg_days=7,
        team_id=args.team_id,
    )
    data = resp.data
    if data is None:
        return {"team_id": args.team_id, "players": [], "note": resp.message or "No matchup on the calendar"}
    return {
        "team_id": args.team_id,
        "matchup": data.matchup_number,
        "start": data.start_date,
        "end": data.end_date,
        "today_index": data.current_day_index,
        "mode": args.mode,
        "day": data.target_day,
        "upcoming": data.upcoming,
        # fpts, or the category value proxy in a categories league
        "value_kind": data.value_kind,
        "pool_size": len(data.streamers),
        "players": [
            {
                "player_id": p.nba_player_id,
                "name": p.name,
                "team": p.team,
                "positions": "/".join(p.valid_positions),
                "avg_value_last_7": p.avg_points_last_n,
                "avg_value_season": p.avg_points_season,
                "games_left": p.games_remaining,
                "game_days": p.game_days,
                "back_to_backs": p.b2b_game_count,
                "stream_score": p.streamer_score,
                "injury": _injury(p.injured, p.injury_status),
                "acquisition": getattr(p.acquisition_status, "value", p.acquisition_status),
                "waivers_until": p.waivers_until,
            }
            for p in data.streamers[: args.limit]
        ],
    }


async def _get_game_log(args: GameLogInput, ctx: ToolContext | None) -> dict[str, Any]:
    resp = await PlayerGamesService.get_player_games(player_id=args.player_id, limit=args.games)
    if resp.status != ApiStatus.SUCCESS or resp.data is None:
        raise AppError("PLAYER_NOT_FOUND", resp.message or f"No player {args.player_id}")
    data = resp.data
    return {
        "player_id": args.player_id,
        "name": data.player_name,
        "games": [g.model_dump(exclude={"game_id"}) for g in data.games],
        "note": resp.message,
    }


async def _get_scheduled_pickups(args: TeamInput, ctx: ToolContext | None) -> dict[str, Any]:
    team, _ = await _owned(ctx, args.team_id)
    data = (await ScheduledPickupService.list_for_team(team)).data

    def row(p: Any) -> dict[str, Any]:
        return {
            "date": p.nba_date,
            "status": getattr(p.status, "value", p.status),
            "add": {"player_id": p.add.nba_player_id, "name": p.add.name, "team": p.add.team},
            "drop": ({"player_id": p.drop.nba_player_id, "name": p.drop.name, "team": p.drop.team}
                     if p.drop else None),
            "reason": p.reason,
            "from_stream_lane": p.lane_id is not None,
        }
    return {
        "team_id": args.team_id,
        "pending": [row(p) for p in (data.pending if data else [])],
        "recent": [row(p) for p in (data.recent if data else [])],
    }


AGENT_GET_MY_TEAMS: dict[str, Any] = {
    **GET_MY_TEAMS,
    "description": (
        "The user's own fantasy teams: team_id, team name, league name, provider, season and "
        "scoring format. Every team-scoped tool takes one of these team_ids."
    ),
}

_TEAM_ID = {"type": "integer", "description": "One of the user's team_ids"}

AGENT_READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_matchup",
        "description": (
            "The team's current head-to-head matchup: dates, both teams' scores and projected "
            "finals, and both rosters (slot, games left this matchup, injury, avg_value over the "
            "window). In a categories league, `categories` gives each category's current and "
            "projected totals for both sides and who leads; `score` and `projected` are then "
            "categories won. avg_value is fantasy points in a points league and a category value "
            "score in a categories league."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": _TEAM_ID,
                "window": {"type": "string", "enum": ["season", "last_7", "last_14", "last_30"],
                           "description": "The window avg_value is averaged over (default season)"},
            },
            "required": ["team_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_free_agents",
        "description": (
            "Free agents in the team's league ranked for streaming, best first: rest-of-matchup "
            "value (mode 'week') or one day's (mode 'day', with `day` the 0-based day of the "
            "matchup; today_index says which day is today). Each has games left, the matchup days "
            "they play (game_days, 0-based), back-to-backs, injury, and whether a pickup is "
            "immediate or a waiver claim. Ranked from the 150 most-owned free agents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": _TEAM_ID,
                "mode": {"type": "string", "enum": ["week", "day"]},
                "day": {"type": "integer", "description": "Day of the matchup, 0-based; mode 'day' only"},
                "limit": {"type": "integer", "description": "How many to return, 1-30 (default 15)"},
                "include_injured": {"type": "boolean"},
                "b2b_only": {"type": "boolean", "description": "Only players with a back-to-back left"},
            },
            "required": ["team_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_game_log",
        "description": (
            "A player's most recent box scores, newest first: date, opponent, minutes, points, "
            "rebounds, assists, steals, blocks, turnovers, makes and attempts, and CV fantasy "
            "points (fpts, Court Vision's own formula, not the user's league scoring)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer", "description": "NBA player_id"},
                "games": {"type": "integer", "description": "How many games, 1-15 (default 5)"},
            },
            "required": ["player_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_scheduled_pickups",
        "description": (
            "The team's scheduled pickups: pending ones (date, add, drop) and those settled in "
            "the last week, with how they ended."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"team_id": _TEAM_ID},
            "required": ["team_id"],
            "additionalProperties": False,
        },
    },
]

# Stats and status (step 0), name lookup, the user's teams, then their leagues.
# The order is part of every request's cached prefix: append, never reorder.
AGENT_TOOLS: list[dict[str, Any]] = [*TOOLS, AGENT_GET_MY_TEAMS, *AGENT_READ_TOOLS]
AGENT_TOOL_NAMES = frozenset(t["name"] for t in AGENT_TOOLS)

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
    "get_matchup": _Tool(MatchupInput, _get_matchup),
    "get_free_agents": _Tool(FreeAgentsInput, _get_free_agents),
    "get_game_log": _Tool(GameLogInput, _get_game_log),
    "get_scheduled_pickups": _Tool(TeamInput, _get_scheduled_pickups),
}
assert ASK_TOOL_NAMES | ROUTER_TOOL_NAMES | AGENT_TOOL_NAMES == set(_REGISTRY), \
    "every defined tool needs a handler, and vice versa"


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
