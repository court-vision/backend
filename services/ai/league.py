"""
Dime's league knowledge (the Dime autopilot plan, step 2): what a manager reads
about the rest of the league before a move.

- `get_league`: the standings, the playoff picture, this matchup's scoreboard,
  the team's whole schedule, and any team's roster, now or on a finished day
  (services/league_overview.py, one ESPN read).
- `get_league_activity`: who has been adding and dropping whom, read off the
  move ledger with no ESPN call.
- `get_draft_board`: a draft room's recommendations for the user's next pick
  with every term of their score, and the best players still on the board
  (services/draft_board_service.py, the board the Draft desk shows).

All three are reads. The tool definitions are appended to `AGENT_TOOLS` after
the move ledger's and never reordered: the list is a cached prefix.
"""

from __future__ import annotations

from datetime import date, timedelta

Day = date      # a field named `date` would shadow the type inside its model
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from api.deps import OwnedDraftSessionContext, OwnedTeamContext
from core.errors import AppError
from db.base import run_db
from schemas.common import FantasyProvider, LeagueInfo
from services import league_overview
from services.lineup_snapshot_service import public_league_info

GET_LEAGUE_NAME = "get_league"
GET_LEAGUE_ACTIVITY_NAME = "get_league_activity"
GET_DRAFT_BOARD_NAME = "get_draft_board"
LEAGUE_TOOL_NAMES = frozenset({GET_LEAGUE_NAME, GET_LEAGUE_ACTIVITY_NAME, GET_DRAFT_BOARD_NAME})

MAX_ACTIVITY_DAYS = 60
BOARD_POSITIONS = ("PG", "SG", "SF", "PF", "C", "G", "F")


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetLeagueInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    rosters: list[int] = Field(default_factory=list, max_length=league_overview.MAX_ROSTERS)
    all_rosters: bool = False
    date: Optional[Day] = None


class LeagueActivityInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    days: int = Field(7, ge=1, le=MAX_ACTIVITY_DAYS)
    league_team_id: Optional[int] = Field(None, gt=0, lt=2**31)
    player_id: Optional[int] = Field(None, gt=0, lt=2**31)
    limit: int = Field(25, ge=1, le=60)


# The board service scores at most this many recommendations (draft_board_service.RECOMMENDATION_COUNT,
# imported lazily with the rest of the board; a test pins the two together).
BOARD_RECOMMENDATIONS = 5


class DraftBoardInput(_Input):
    room_id: int = Field(..., gt=0, lt=2**31)
    rank_source: Literal["cv", "espn"] = "cv"
    recommendations: int = Field(BOARD_RECOMMENDATIONS, ge=1, le=BOARD_RECOMMENDATIONS)
    available: int = Field(10, ge=0, le=30)
    position: Optional[Literal["PG", "SG", "SF", "PF", "C", "G", "F"]] = None


_TEAM_ID = {"type": "integer", "description": "One of the user's team_ids"}

GET_LEAGUE: dict[str, Any] = {
    "name": GET_LEAGUE_NAME,
    "description": (
        "The team's whole league, from ESPN now: the standings (rank, wins, losses, ties, win_pct, "
        "games_back of first, streak, points for and against in a points league, adds this matchup "
        "and this season, waiver rank), the playoff picture (how many teams make it, and for each "
        "team whether it holds a spot today and how many games it leads the first team out or "
        "trails the last team in), this matchup's scoreboard (every pairing and both scores as ESPN "
        "reports them), and my_schedule: every matchup of the user's team with its dates, the "
        "opponent and his rank today, and for a finished one the result and both scores. A category "
        "league counts categories won, lost and tied. Each team has a league_team_id; give some in "
        "`rosters` (or all_rosters) for their players (NBA player_id, team, positions, slot, injury, "
        "avg_value under the league's scoring), as ESPN has them now, or as they stood on a finished "
        "`date` (with each player's points that day). ESPN leagues only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": _TEAM_ID,
            "rosters": {"type": "array", "items": {"type": "integer"},
                        "description": "league_team_ids from the standings whose rosters to include"},
            "all_rosters": {"type": "boolean", "description": "Every team's roster (a long result)"},
            "date": {"type": "string", "format": "date",
                     "description": "YYYY-MM-DD: the rosters as they stood that finished day instead of now"},
        },
        "required": ["team_id"],
        "additionalProperties": False,
    },
}

GET_LEAGUE_ACTIVITY: dict[str, Any] = {
    "name": GET_LEAGUE_ACTIVITY_NAME,
    "description": (
        "What the managers in the team's league have added and dropped over the last `days` days "
        "(default 7), read off every team's nightly rosters, so it runs through `through`, the last "
        "stored day (usually last night): today's moves arrive tomorrow. by_team says, busiest first, "
        "each team's adds and drops in the window, how many of its adds it still holds, the positions "
        "it added and its latest move. moves lists them newest first: the day it counted, the team, "
        "the player added and dropped (NBA player_id, position) and whether the add is still on that "
        "team (held_through when he was let go again). Who is streaming, who is thin at a position, "
        "and who competes for the same free agents. Filter to one team (league_team_id from "
        "get_league) or one player. Rivals' moves carry no source; how each went is "
        "get_move_outcomes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": _TEAM_ID,
            "days": {"type": "integer", "minimum": 1, "maximum": MAX_ACTIVITY_DAYS,
                     "description": "How far back, in days (default 7)"},
            "league_team_id": {"type": "integer", "description": "Only this team's moves (from get_league)"},
            "player_id": {"type": "integer", "description": "Only moves that added or dropped this NBA player_id"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60,
                      "description": "Moves to list, newest first (default 25); by_team covers all"},
        },
        "required": ["team_id"],
        "additionalProperties": False,
    },
}

GET_DRAFT_BOARD: dict[str, Any] = {
    "name": GET_DRAFT_BOARD_NAME,
    "description": (
        "One of the user's draft rooms as its board scores it: the recommendations for the user's "
        "next pick, best first, each with its score and every term of it in season-value points "
        "(value over replacement, punts, injury, lineup congestion; category_fit is shown but not "
        "summed), its value, CV and ESPN ranks, whether he is likely to last to that pick "
        "(availability: likely, tossup or gone) and a one-line reason; then the best players still "
        "on the board in its own order (optionally one position), the user's drafted roster, the "
        "room's punts and, in a category league, how far the roster trails an average team in each "
        "category. rank_source espn orders the recommendations by ESPN's board instead, with the same "
        "numbers on each. Cite these numbers when advising a pick."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "room_id": {"type": "integer", "description": "A room_id from get_draft_rooms"},
            "rank_source": {"type": "string", "enum": ["cv", "espn"],
                            "description": "What orders the recommendations (default cv)"},
            "recommendations": {"type": "integer", "minimum": 1, "maximum": BOARD_RECOMMENDATIONS,
                                "description": "How many recommendations (default 5, the most the board scores)"},
            "available": {"type": "integer", "minimum": 0, "maximum": 30,
                          "description": "How many of the best players still on the board (default 10)"},
            "position": {"type": "string", "enum": list(BOARD_POSITIONS),
                         "description": "Only players eligible at this position in the best-available list"},
        },
        "required": ["room_id"],
        "additionalProperties": False,
    },
}

LEAGUE_TOOLS: list[dict[str, Any]] = [GET_LEAGUE, GET_LEAGUE_ACTIVITY, GET_DRAFT_BOARD]


# ---- get_league ----

async def get_league(team: OwnedTeamContext, league_info: LeagueInfo, args: GetLeagueInput) -> dict[str, Any]:
    out = await league_overview.read_league(league_info, roster_ids=args.rosters, all_rosters=args.all_rosters,
                                            day=args.date)
    return {"team_id": team.team_id, **out}


# ---- get_league_activity ----

async def get_league_activity(team: OwnedTeamContext, args: LeagueActivityInput, *,
                              today: Optional[date] = None) -> dict[str, Any]:
    from services import move_ledger
    from services.matchup_days import fantasy_today

    info = public_league_info(team)
    if info.provider != FantasyProvider.ESPN:
        raise AppError("LEAGUE_ESPN_ONLY", "League activity is read for ESPN leagues only, and this is a Yahoo team")
    key = move_ledger.league_key_of(info)
    own = await run_db("league.own_team", move_ledger.own_provider_team_id, key, info)
    today = today or fantasy_today()
    q = league_overview.ActivityQuery(
        team_id=team.team_id, provider=key.provider, provider_league_id=key.provider_league_id, season=key.season,
        provider_team_id=own, since=today - timedelta(days=args.days), league_team_id=args.league_team_id,
        nba_player_id=args.player_id, limit=args.limit,
    )
    found = await run_db("league.activity", league_overview.read_activity, q)
    out = {"team_id": team.team_id, "days": args.days, **found}
    if found["through"] is None:
        out["note"] = "No rosters are stored for this league yet, so there is no activity to read"
    return out


# ---- get_draft_board ----

def _component(c: Any) -> dict[str, Any]:
    return {"key": c.key, "label": c.label, "value": round(c.value, 1), "in_score": c.in_score, "detail": c.detail}


def _eligible(row: Any, position: Optional[str]) -> bool:
    if position is None:
        return True
    slots = set(row.positions or [])
    if position in ("G", "F"):
        return position in slots or bool(slots & ({"PG", "SG"} if position == "G" else {"SF", "PF"}))
    return position in slots or (not slots and row.primary_position == position)


async def get_draft_board(room: OwnedDraftSessionContext, args: DraftBoardInput) -> dict[str, Any]:
    from services.draft_board_service import BoardSession, DraftBoardService
    from services.draft_service import DraftService
    from services.scoring.resolver import resolve_scoring_for_room

    detail = (await DraftService.get_session(room.session_id)).data
    scoring = resolve_scoring_for_room(room.league, room.scoring_format)
    board = await DraftBoardService.get_board(
        scoring, session=BoardSession.of(room, rank_source=args.rank_source, board_source="espn"))
    meta = board.meta
    rows = list(board.data or [])
    if meta is None or not rows:
        return {"room_id": room.session_id, "recommendations": [], "best_available": [],
                "note": board.message or "The board has no players to score yet"}

    by_id = {r.player_id: r for r in rows}
    recommendations = []
    for rec in board.recommendations[: args.recommendations]:
        row = by_id.get(rec.player_id)
        recommendations.append({
            "player_id": rec.player_id,
            "name": rec.name,
            "position": rec.primary_position,
            "team": row.team if row else None,
            "score": round(rec.score, 1),
            "value": round(rec.value, 2),
            "season_value": round(rec.season_value, 1),
            "vorp": round(rec.vorp, 1),
            "cv_rank": rec.cv_rank,
            "market_rank": rec.market_rank,
            "adp": row.adp if row else None,
            "availability": row.availability if row else None,
            "injury": row.injury_status if row else None,
            "components": [_component(c) for c in rec.components],
            "reason": rec.reason,
        })

    available = []
    for row in rows:
        if len(available) >= args.available:
            break
        if not _eligible(row, args.position):
            continue
        entry: dict[str, Any] = {
            "board_rank": row.board_rank,
            "player_id": row.player_id,
            "name": row.name,
            "team": row.team,
            "positions": "/".join(p for p in (row.positions or []) if p in ("PG", "SG", "SF", "PF", "C"))
                         or row.primary_position,
            "value": round(row.value, 2) if row.value is not None else None,
            "value_source": row.value_source,
            "cv_rank": row.cv_rank,
            "market_rank": row.market_rank,
            "adp": row.adp,
            "room_score": round(row.room_score, 1) if row.room_score is not None else None,
            "room_rank": row.room_rank,
            "availability": row.availability,
            "injury": row.injury_status,
            "cap_blocked": row.cap_blocked,
        }
        if row.fit_rank is not None:
            entry["fit_rank"] = row.fit_rank
        available.append(entry)

    out: dict[str, Any] = {
        "room_id": room.session_id,
        "name": detail.name if detail else None,
        "status": getattr(detail.status, "value", detail.status) if detail else None,
        "format": meta.format,
        "value_kind": meta.value_kind,
        "picks_made": detail.pick_count if detail else None,
        "my_next_pick": detail.my_next_pick if detail else None,
        "picks_until_my_turn": detail.picks_until_my_turn if detail else None,
        "recommendations_by": meta.rank_source,
        "board_order": meta.rank_basis,
        "punts": list(meta.punts),
        "recommendations": recommendations,
        "best_available": available,
        "roster": [
            {"player_id": r.player_id, "name": r.name, "team": r.team, "position": r.primary_position,
             "value": round(r.value, 2) if r.value is not None else None}
            for r in board.roster
        ],
    }
    if meta.category_need:
        out["category_need"] = [
            {"category": n.key, "label": n.label, "need": round(n.need, 2), "punted": n.punted,
             "my_rank": n.my_rank, "of": n.seats}
            for n in meta.category_need
        ]
    if args.position:
        out["position"] = args.position
    return out
