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
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.deps import OwnedTeamContext, ensure_team_owned, load_owned_league_info
from core.errors import AppError
from core.logging import get_logger
from db.base import db_operation, run_db
from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from schemas.common import ApiStatus, LeagueInfo
from schemas.streamer import StreamerMode
from core.nba_calendar import nba_date_et
from core.settings import settings
from services.draft_service import DraftService
from services.game_context import GameContext
from services.matchup_service import MatchupService
from services.public_market_service import PublicMarketService, _snapshot_date
from services.schedule_service import get_matchup_by_number, get_streaming_matchup
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


STAT_KEYS = ("pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "fgm", "fga", "ftm", "fta", "fg3a", "min", "fpts")
StatKey = Literal["pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "fgm", "fga", "ftm", "fta", "fg3a", "min", "fpts"]
SEASON_PATTERN = r"^20[0-9]{2}-[0-9]{2}$"


class GameLogInput(_Input):
    player_id: int = Field(..., gt=0, lt=2**31)
    games: int = Field(5, ge=1, le=82)
    season: Optional[str] = Field(None, pattern=SEASON_PATTERN)
    season_type: Literal["regular", "playoffs", "play_in", "all"] = "regular"
    sort_by: Union[Literal["date"], StatKey] = "date"


class ProjectionsInput(_Input):
    player_ids: list[int] = Field(default_factory=list, max_length=30)
    sort_by: Optional[StatKey] = None
    limit: int = Field(15, ge=1, le=50)


class DraftMarketInput(_Input):
    player_ids: list[int] = Field(default_factory=list, max_length=30)
    sort_by: Literal["rank", "adp", "auction_value", "auction_value_avg"] = "adp"
    limit: int = Field(25, ge=1, le=60)


class MarketMovementInput(_Input):
    days: int = Field(7, ge=1, le=60)
    metric: Literal["rank", "adp", "auction_value", "auction_value_avg"] = "adp"
    direction: Literal["up", "down", "both"] = "both"
    limit: int = Field(20, ge=1, le=50)


class ScheduleInput(_Input):
    teams: list[str] = Field(default_factory=list, max_length=30)
    week: Literal["current", "next"] = "current"
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class DraftRoomInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)


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


_SLOT_PREFIXES = ("UT", "BE", "IR")


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
                # Positions only: ESPN's list also names the slots (UT1, BE, IR)
                "positions": "/".join(pos for pos in p.valid_positions if not pos.startswith(_SLOT_PREFIXES)),
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


_GAME_FIELDS = ("min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "fpts")


@db_operation("ai.game_log")
def _game_log(player_id: int, games: int, season: Optional[str], season_type: str, sort_by: str) -> dict[str, Any]:
    player = Player.get_or_none(Player.id == player_id)
    if player is None:
        raise AppError("PLAYER_NOT_FOUND", f"No player {player_id}")
    played = (PlayerGameStats.player == player_id) & (PlayerGameStats.min > 0)
    if season_type != "all":
        played &= PlayerGameStats.season_type == season_type
    if season is None:
        # The latest season he has games in: last season's until this one starts
        season = (PlayerGameStats.select(PlayerGameStats.season).where(played)
                  .order_by(PlayerGameStats.game_date.desc()).limit(1).scalar())
        if season is None:
            return {"player_id": player_id, "name": player.name, "season": None, "games": [],
                    "note": "No games on record for this player"}
    query = PlayerGameStats.select().where(played & (PlayerGameStats.season == season))
    total = query.count()
    if sort_by == "date":
        query = query.order_by(PlayerGameStats.game_date.desc())
    else:
        query = query.order_by(getattr(PlayerGameStats, sort_by).desc(), PlayerGameStats.game_date.desc())
    rows = list(query.limit(games))
    fixtures = GameContext.for_rows(rows)
    out = []
    for r in rows:
        fx = fixtures.of(r)
        out.append({
            "date": r.game_date,
            "opponent": fx.get("opponent"),
            "home": fx.get("home"),
            "type": r.season_type,
            **{k: getattr(r, k) for k in _GAME_FIELDS},
        })
    return {
        "player_id": player_id,
        "name": player.name,
        "season": season,
        "season_type": season_type,
        "sort_by": sort_by,
        # Games played that season (sub-minute appearances left out), before the limit
        "games_played": total,
        "games": out,
    }


async def _get_game_log(args: GameLogInput, ctx: ToolContext | None) -> dict[str, Any]:
    return await _game_log(args.player_id, args.games, args.season, args.season_type, args.sort_by)


def _num(value: Any) -> Any:
    """Numeric columns come back as Decimal, which JSON would write as a string."""
    return float(value) if isinstance(value, Decimal) else value


def _projection_row(row: Any) -> dict[str, Any]:
    stats = {k: _num(getattr(row, k)) for k in PlayerProjection.STAT_KEYS}
    for rate, made, attempted in (("fg_pct", "fgm", "fga"), ("fg3_pct", "fg3m", "fg3a"), ("ft_pct", "ftm", "fta")):
        a = stats.get(attempted)
        stats[rate] = round(stats[made] / a, 3) if a and stats.get(made) is not None else None
    return {"player_id": row.player_id, "name": row.player.name, "projected_gp": row.projected_gp, **stats}


@db_operation("ai.projections")
def _projections(player_ids: list[int], sort_by: Optional[str], limit: int) -> dict[str, Any]:
    season = settings.nba_season
    snapshot = _snapshot_date(PlayerProjection, season)
    if snapshot is None:
        return {"season": season, "players": [], "note": f"No ESPN projections for {season} yet"}
    query = (PlayerProjection.select(PlayerProjection, Player).join(Player)
             .where((PlayerProjection.season == season) & (PlayerProjection.source == "espn")
                    & (PlayerProjection.as_of_date == snapshot)))
    if player_ids:
        query = query.where(PlayerProjection.player.in_(player_ids))
    if sort_by and sort_by != "fpts" and hasattr(PlayerProjection, sort_by):
        query = query.order_by(getattr(PlayerProjection, sort_by).desc(nulls="LAST"), Player.name)
    else:
        query = query.order_by(Player.name)
    rows = list(query.limit(len(player_ids) if player_ids else limit))
    return {
        "season": season,
        "source": "ESPN season projections",
        "as_of": snapshot,
        "per": "game",
        "players": [_projection_row(r) for r in rows],
    }


async def _get_projections(args: ProjectionsInput, ctx: ToolContext | None) -> dict[str, Any]:
    if not args.player_ids and not args.sort_by:
        raise AppError("AI_BAD_INPUT", "Give player_ids, or sort_by to list the top projected players")
    return await _projections(args.player_ids, args.sort_by, args.limit)


def _market_row(p: Any) -> dict[str, Any]:
    return {
        "player_id": p.player_id,
        "name": p.name,
        "espn_rank": p.overall_rank,
        "adp": p.adp,
        "auction_value": p.auction_value,
        "auction_avg": p.auction_value_avg,
        "injury": p.injury_status if p.injury_status not in (None, "ACTIVE") else None,
    }


async def _get_draft_market(args: DraftMarketInput, ctx: ToolContext | None) -> dict[str, Any]:
    # The whole board when filtering by player (ESPN lists a few hundred), the top otherwise
    resp = await PublicMarketService.get_market(sort_by=args.sort_by, limit=500 if args.player_ids else args.limit)
    data = resp.data
    if data is None or not data.players:
        return {"players": [], "note": resp.message}
    players = data.players
    if args.player_ids:
        wanted = set(args.player_ids)
        players = [p for p in players if p.player_id in wanted]
    return {
        "season": data.season,
        "source": "ESPN draft market",
        "as_of": data.as_of_date,
        "sort_by": args.sort_by,
        "players": [_market_row(p) for p in players],
    }


async def _get_market_movement(args: MarketMovementInput, ctx: ToolContext | None) -> dict[str, Any]:
    today = nba_date_et()
    resp = await PublicMarketService.get_market_movement(
        from_as_of=today - timedelta(days=args.days), to_as_of=today,
        metric=args.metric, direction=args.direction, limit=args.limit)
    data = resp.data
    if data is None:
        return {"players": [], "note": resp.message}
    return {
        "season": data.season,
        "from": data.from_as_of_date,
        "to": data.to_as_of_date,
        "metric": args.metric,
        # Positive change = moving earlier (rising); negative = falling
        "players": [
            {
                "player_id": p.player_id,
                "name": p.name,
                "adp_before": p.before.adp if p.before else None,
                "adp_after": p.after.adp if p.after else None,
                "rank_before": p.before.overall_rank if p.before else None,
                "rank_after": p.after.overall_rank if p.after else None,
                "adp_change": p.changes.adp,
                "rank_change": p.changes.overall_rank,
            }
            for p in data.players
        ],
        "note": resp.message,
    }


MAX_SCHEDULE_DAYS = 28


@db_operation("ai.schedule")
def _schedule(teams: list[str], start: date, end: date) -> dict[str, Any]:
    games = list(Game.select().where((Game.game_date >= start) & (Game.game_date <= end)
                                     & (Game.season_type.is_null() | (Game.season_type != "preseason")))
                 .order_by(Game.game_date, Game.start_time_et))
    per_day: dict[str, int] = {}
    by_team: dict[str, list[dict[str, Any]]] = {}
    for g in games:
        day = g.game_date.isoformat()
        per_day[day] = per_day.get(day, 0) + 1
        for team, opp, home in ((g.home_team_id, g.away_team_id, True), (g.away_team_id, g.home_team_id, False)):
            by_team.setdefault(team, []).append({"date": day, "opponent": opp, "home": home})
    wanted = [t.upper() for t in teams] or sorted(by_team)
    return {
        "start": start,
        "end": end,
        "games_per_day": [{"date": d, "games": n} for d, n in sorted(per_day.items())],
        "teams": [
            {"team": t, "games": len(by_team.get(t, [])),
             "dates": ", ".join(x["date"][5:] for x in by_team.get(t, [])), "schedule": by_team.get(t, [])}
            for t in wanted
        ],
    }


async def _get_schedule(args: ScheduleInput, ctx: ToolContext | None) -> dict[str, Any]:
    if args.start_date or args.end_date:
        start = args.start_date or nba_date_et()
        end = args.end_date or start + timedelta(days=6)
        label = "dates given"
    else:
        week = get_streaming_matchup()
        if week is not None and args.week == "next":
            week = get_matchup_by_number(int(week["matchup_number"]) + 1)
        if week is None:
            raise AppError("AI_NO_WEEK", "No fantasy week on the calendar for that")
        start, end = week["start_date"], week["end_date"]
        label = f"fantasy week {week['matchup_number']}"
    if end < start or (end - start).days >= MAX_SCHEDULE_DAYS:
        raise AppError("AI_BAD_INPUT", f"Give a range of 1-{MAX_SCHEDULE_DAYS} days")
    result = await _schedule(args.teams, start, end)
    return {"range": label, **result}


async def _get_draft_rooms(args: MyTeamsInput, ctx: ToolContext | None) -> dict[str, Any]:
    if ctx is None:
        raise AppError("AI_NO_CALLER", "This lookup needs a signed-in user")
    resp = await DraftService.list_sessions(ctx.user_id)
    return {
        "rooms": [
            {
                "room_id": r.id,
                "name": r.name,
                "kind": getattr(r.kind, "value", r.kind),
                "status": getattr(r.status, "value", r.status),
                "scoring": r.scoring_format,
                "teams": r.league_size,
                "rounds": r.rounds,
                "my_slot": r.my_slot,
                "picks_made": r.pick_count,
                "next_pick": r.next_overall_pick,
                "my_next_pick": r.my_next_pick,
                "team_id": r.team_id,
                "updated": r.updated_at,
            }
            for r in (resp.data or [])
        ]
    }


async def _get_draft_room(args: DraftRoomInput, ctx: ToolContext | None) -> dict[str, Any]:
    from api.deps import _owned_session

    if ctx is None:
        raise AppError("AI_NO_CALLER", "This lookup needs a signed-in user")
    if await run_db("ai.owned_session", _owned_session, args.room_id, ctx.user_id) is None:
        raise AppError("DRAFT_NOT_FOUND", f"Draft room {args.room_id} is not one of the user's rooms")
    room = (await DraftService.get_session(args.room_id)).data
    return {
        "room_id": room.id,
        "name": room.name,
        "status": getattr(room.status, "value", room.status),
        "scoring": room.scoring_format,
        "teams": room.league_size,
        "rounds": room.rounds,
        "my_slot": room.my_slot,
        "punts": room.punts,
        "picks_made": room.pick_count,
        "my_next_pick": room.my_next_pick,
        "picks_until_my_turn": room.picks_until_my_turn,
        "picks": [
            {"pick": p.overall_pick, "round": p.round, "slot": p.slot, "player_id": p.player_id,
             "name": p.player_name, "mine": p.by_me, "keeper": getattr(p.source, "value", p.source) == "keeper"}
            for p in room.picks
        ],
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
            "A player's box scores from Court Vision's game history (every season from "
            "2012-13). By default his latest season's most recent games, newest first; give "
            "`season` for another one, and `sort_by` a stat for his best games in it (his top 10 "
            "blocks games: sort_by 'blk', games 10). Up to 82 games. Each has date, opponent, home, "
            "minutes, points, rebounds, assists, steals, blocks, turnovers, makes and attempts, "
            "and CV fantasy points (fpts, Court Vision's formula, not the user's league scoring). "
            "games_played is how many he played that season. Appearances under a minute are left out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer", "description": "NBA player_id"},
                "games": {"type": "integer", "description": "How many games, 1-82 (default 5)"},
                "season": {"type": "string", "description": "e.g. '2025-26'; default his latest season"},
                "season_type": {"type": "string", "enum": ["regular", "playoffs", "play_in", "all"]},
                "sort_by": {"type": "string", "enum": ["date", *STAT_KEYS],
                            "description": "'date' (newest first, default) or a stat (highest first)"},
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

_PLAYER_IDS = {"type": "array", "items": {"type": "integer"}, "description": "NBA player_ids, at most 30"}
_MARKET_SORT = ["rank", "adp", "auction_value", "auction_value_avg"]

# Added 2026-10-15. Appended after the first set, never interleaved: the order is
# part of every request's prefix.
AGENT_MORE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_projections",
        "description": (
            "ESPN's season projections, per game: projected games played and every stat (shooting "
            "rates from projected makes and attempts). Give player_ids, or sort_by a stat for the "
            "top projected players in it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_ids": _PLAYER_IDS,
                "sort_by": {"type": "string", "enum": [k for k in STAT_KEYS if k != "fpts"]},
                "limit": {"type": "integer", "description": "1-50 (default 15), with sort_by"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_draft_market",
        "description": (
            "ESPN's draft market, from its latest daily snapshot: editorial rank, ADP (average "
            "pick in real ESPN drafts), auction value and average auction price. Give player_ids "
            "for those players, or sort_by for the top of the board."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_ids": _PLAYER_IDS,
                "sort_by": {"type": "string", "enum": _MARKET_SORT},
                "limit": {"type": "integer", "description": "1-60 (default 25)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_market_movement",
        "description": (
            "Who rose and fell in ESPN's draft market over the last `days` days. A positive "
            "change means moving earlier (rising); negative means falling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "1-60 (default 7)"},
                "metric": {"type": "string", "enum": _MARKET_SORT},
                "direction": {"type": "string", "enum": ["up", "down", "both"]},
                "limit": {"type": "integer", "description": "1-50 (default 20)"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_schedule",
        "description": (
            "NBA games over a fantasy week (the current one by default, or `next`) or given "
            "dates (up to 28 days): games per day league-wide, and for each team asked about "
            "(NBA abbreviations; all 30 when none) its game count, dates and opponents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "teams": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['DET', 'UTA']"},
                "week": {"type": "string", "enum": ["current", "next"]},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, instead of a week"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_draft_rooms",
        "description": (
            "The user's draft rooms in Court Vision's Draft desk, newest first: room_id, name, "
            "live or mock, status, scoring, teams, rounds, their slot, picks made, and their next pick."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_draft_room",
        "description": (
            "One of the user's draft rooms with every pick so far (pick, round, slot, player, and "
            "whether it was the user's), plus their slot, punts and next pick."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"room_id": {"type": "integer", "description": "A room_id from get_draft_rooms"}},
            "required": ["room_id"],
            "additionalProperties": False,
        },
    },
]

# Stats and status (step 0), name lookup, the user's teams, then their leagues.
# The order is part of every request's cached prefix: append, never reorder.
AGENT_TOOLS: list[dict[str, Any]] = [*TOOLS, AGENT_GET_MY_TEAMS, *AGENT_READ_TOOLS, *AGENT_MORE_TOOLS]
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
    "get_projections": _Tool(ProjectionsInput, _get_projections),
    "get_draft_market": _Tool(DraftMarketInput, _get_draft_market),
    "get_market_movement": _Tool(MarketMovementInput, _get_market_movement),
    "get_schedule": _Tool(ScheduleInput, _get_schedule),
    "get_draft_rooms": _Tool(MyTeamsInput, _get_draft_rooms),
    "get_draft_room": _Tool(DraftRoomInput, _get_draft_room),
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
