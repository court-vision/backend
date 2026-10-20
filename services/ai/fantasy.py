"""
The agent's fantasy logic (the CV AI plan, § 5): the two tools that answer "what if"
and "who should I stream", over the projection and the planners the Week desk uses.

- `project_matchup`: today's matchup projection with roster moves applied. The
  matchup read (`MatchupService.get_matchup_by_team_id`) carries each player's
  per-game value and line; this module turns the roster into `SidePlayer`s with
  their games per day off the season calendar, resolves the adds by NBA id, picks
  the drop when an add has none and the roster is full (the daily swap's rule), and
  scores before and after with `services.matchup_projection`.
- `plan_streams`: the joint plan for one to three streamed spots over the days left
  (`services.multi_stream_planner`, built as the stream lanes build theirs), under
  a value function of the agent's choosing (`services.stream_value`): the league's
  own measure, one category, or the matchup's close categories.

Timing (the ESPN rules the lanes follow): the fantasy day rolls over at 02:00 ET; a
move made after the day's first tip-off counts from the next day. Every id the
tools take or return is an NBA player id; provider ids never leave the server.

Known simplification (v1): lineup slots are ignored, as in the matchup projection
-- every counting player is assumed to start every game he has.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.compute import run_cpu
from core.errors import AppError
from core.logging import get_logger
from db.base import run_db
from db.models.nba.games import Game
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from schemas.common import FantasyProvider, LeagueInfo
from services import schedule_service
from services.acquisitions import adds_for_day
from services.daily_actions_service import BOARD_WINDOW_DAYS, MIN_SAMPLE_GAMES, DropEvidence, DropValue, weigh_drop
from services.espn_service import EspnService, PoolEntry
from services.lineup_editor_service import _roster_slots
from services.lineup_planner import IR_SLOT_ID, OUT_STATUSES
from services.matchup_projection import (
    DropCandidate,
    Move,
    MoveInvalid,
    SidePlayer,
    apply_moves,
    category_projection,
    choose_drop,
    games_counted,
    points_projection,
)
from services.matchup_service import MatchupService
from services.player_service import _normalize_name
from services.player_value_service import PlayerValueService
from services.providers import get_provider_adapter
from services.providers.identity import nba_ids_by_espn_id
from services.scheduled_pickup_service import et_at
from services.scoring.category_rank import compute_category_scores
from services.scoring.category_value import rankable_categories
from services.scoring.models import CategoryTeamScoreData, StatLine
from services.stream_lane_service import (
    StreamLaneService,
    _days,
    _pending_pickups,
    first_open_period,
    lane_budget,
    plan_spots,
)
from services.stream_value import (
    LEAGUE,
    MATCHUP,
    VALUE_KINDS,
    VALUE_LABELS,
    category_rate,
    matchup_weights,
    pool_stds,
    signed_margin,
    weighted_category_value,
)
from utils.espn_helpers import normalize_nba_abbrev

log = get_logger("ai.fantasy")

MAX_MOVES = 6
MAX_SPOTS = 3
MAX_NAMED = 30
# Free agents the planner sees: the best under the value function, plus the ones named.
POOL_CAP = 80
NBA_TEAMS = 30
# What the add's value and line are read over when the matchup read has none for him.
ADD_LINE_DAYS = 7
MATCHUP_DAYS = 14


# ------------------------------- inputs ------------------------------- #


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveInput(_Input):
    add: Optional[int] = Field(None, gt=0, description="NBA player_id to add")
    drop: Optional[int] = Field(None, gt=0, description="NBA player_id to drop")
    day: Optional[int] = Field(None, ge=0, le=13)

    @model_validator(mode="after")
    def _something_moves(self) -> "MoveInput":
        if self.add is None and self.drop is None:
            raise ValueError("a move needs an add, a drop, or both")
        if self.add is not None and self.add == self.drop:
            raise ValueError("a move can't add and drop the same player")
        return self


class ProjectMatchupInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    moves: list[MoveInput] = Field(..., min_length=1, max_length=MAX_MOVES)
    window: Literal["season", "last_7", "last_14", "last_30"] = "season"


ValueKind = Literal["league", "matchup", "pts", "reb", "ast", "stl", "blk", "fg3m"]
assert set(ValueKind.__args__) == set(VALUE_KINDS)


class PlanStreamsInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    value: ValueKind = LEAGUE
    spots: int = Field(1, ge=1, le=MAX_SPOTS)
    holders: list[int] = Field(default_factory=list, max_length=MAX_SPOTS)
    candidates: list[int] = Field(default_factory=list, max_length=MAX_NAMED)


# ------------------------------- the tool definitions ------------------------------- #


_TEAM_ID = {"type": "integer", "description": "One of the user's team_ids"}
_NBA_ID = {"type": "integer", "description": "NBA player_id"}

PROJECT_MATCHUP: dict[str, Any] = {
    "name": "project_matchup",
    "description": (
        "What the team's current matchup projection becomes with roster moves applied. Each "
        "move adds a player, drops one, or both, on a matchup day (0 = the matchup's first day; "
        "`today` and `first_open_day` say where the week stands). A dropped player stops "
        "counting from that day and an added player counts from it, each for the games his NBA "
        "team has left; a day earlier than the first open one is moved to it, as ESPN would. "
        "An add with no drop on a full roster gets the drop the rule picks (the player whose "
        "games left are worth the least), which `moves` reports. Several moves apply in day "
        "order to one roster, so a player one move adds can be dropped by a later one (a "
        "stream). Returns the projection before and after: points, or in a categories league "
        "categories won plus each category's projected totals, the leader and whether the moves "
        "flip it. The before figures are get_matchup's own projection. Assumes every counting "
        "player starts every game he has."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": _TEAM_ID,
            "moves": {
                "type": "array", "minItems": 1, "maxItems": MAX_MOVES,
                "description": "The moves to apply, each with an add, a drop, or both",
                "items": {
                    "type": "object",
                    "properties": {
                        "add": {**_NBA_ID, "description": "NBA player_id to add (a free agent)"},
                        "drop": {**_NBA_ID, "description": "NBA player_id to drop (on the roster, or added by an earlier move)"},
                        "day": {"type": "integer", "minimum": 0, "maximum": 13,
                                "description": "The matchup day the move counts from (default: the first open day)"},
                    },
                    "additionalProperties": False,
                },
            },
            "window": {"type": "string", "enum": ["season", "last_7", "last_14", "last_30"],
                       "description": "The window the roster's per-game values are averaged over (default season)"},
        },
        "required": ["team_id", "moves"],
        "additionalProperties": False,
    },
}

PLAN_STREAMS: dict[str, Any] = {
    "name": "plan_streams",
    "description": (
        "The best streaming plan for one to three roster spots over the days left in the "
        "matchup: who should hold each spot on each day, the switches to make and when, within "
        "the league's add limits and waiver rules (ESPN leagues only). `value` is what a player "
        "is worth per game: 'league' (the league's own measure, as get_matchup values players), "
        "one counting category ('blk' to stream for blocks), or 'matchup' (in a categories "
        "league, the category value re-weighted toward the categories the projected matchup is "
        "close in, so a runaway category counts little and a dead heat counts most; `weights` "
        "shows them). `holders` names the roster players whose spots are streamed; by default "
        "the plan streams the `spots` players the drop rule would give up first. `candidates` "
        "are free agents the plan must consider besides the best ones it reads itself. Returns "
        "the holders, each day's holder per spot with his value that day, the switches in day "
        "order (day 0 = today's, to make now), and the plan's value against keeping the holders. "
        "Values are per game under `value`, not fantasy points, unless value is 'league' in a "
        "points league."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": _TEAM_ID,
            "value": {"type": "string", "enum": list(VALUE_KINDS),
                      "description": "What a player is worth per game (default 'league')"},
            "spots": {"type": "integer", "minimum": 1, "maximum": MAX_SPOTS,
                      "description": "How many roster spots to stream (default 1); ignored when holders are named"},
            "holders": {"type": "array", "items": _NBA_ID, "maxItems": MAX_SPOTS,
                        "description": "Roster players whose spots to stream (default: the drop rule's picks)"},
            "candidates": {"type": "array", "items": _NBA_ID, "maxItems": MAX_NAMED,
                           "description": "Free agents the plan must consider"},
        },
        "required": ["team_id"],
        "additionalProperties": False,
    },
}

AGENT_FANTASY_TOOLS: list[dict[str, Any]] = [PROJECT_MATCHUP, PLAN_STREAMS]

LABELS: dict[str, str] = {
    "day": "Day", "date": "Date", "add": "Add", "add_team": "Add team", "add_games": "Add games",
    "drop": "Drop", "drop_team": "Drop team", "drop_games": "Drop games", "drop_chosen": "Drop by rule",
    "projected_you_after": "Proj you after", "leader_after": "Leader after", "flips": "Flips",
    "spot": "Spot", "value": "Value", "switch": "Switch", "games_left": "Games left",
    "value_per_game": "Per game", "chosen_by": "Chosen by", "margin": "Margin", "weight": "Weight",
    "drop_name": "Drop", "add_name": "Add", "availability": "Available",
}


# ------------------------------- shared lookups ------------------------------- #


@dataclass(frozen=True)
class _Identity:
    nba_id: int
    name: str
    espn_id: Optional[int]
    team: Optional[str]


def _identities(nba_ids: Sequence[int]) -> dict[int, _Identity]:
    """Name, ESPN id and current NBA team (the season's latest stats row) for NBA ids."""
    if not nba_ids:
        return {}
    ids = sorted(set(nba_ids))
    players = {p.id: p for p in Player.select(Player.id, Player.name, Player.espn_id).where(Player.id.in_(ids))}
    teams: dict[int, str] = {}
    latest = PlayerSeasonStats.latest_per_player(PlayerSeasonStats.available_season())
    for row in latest.where(PlayerSeasonStats.player.in_(ids)):
        if row.team_id:
            teams[row.player_id] = normalize_nba_abbrev(row.team_id)
    return {pid: _Identity(pid, p.name, p.espn_id, teams.get(pid)) for pid, p in players.items()}


def _first_open_day(today: date, today_index: int, now: datetime) -> int:
    """The first matchup day a move made now counts for: today until its first tip-off, then tomorrow."""
    tip = Game.get_earliest_game_time_on_date(today)
    return today_index + 1 if tip is not None and now >= et_at(today, tip) else today_index


def _game_days(week: Mapping[str, Any], team: Optional[str], from_day: int) -> tuple[int, ...]:
    """The matchup days (0-based) from `from_day` on that `team` plays, off the calendar the projection reads."""
    if not team:
        return ()
    games = week["games"].get(team) or week["games"].get(normalize_nba_abbrev(team)) or {}
    return tuple(sorted(int(d) for d in games if int(d) >= from_day))


def _drop_evidence(league_info: LeagueInfo, espn_ids: list[int]) -> DropEvidence:
    scoring = PlayerValueService.scoring_for(league_info)
    return DropEvidence(
        games=PlayerValueService.rolling_games_by_espn_id(espn_ids, BOARD_WINDOW_DAYS),
        projected=PlayerValueService.projected_value_by_espn_id(scoring, espn_ids, days=BOARD_WINDOW_DAYS),
    )


async def _evidence_for(league_info: LeagueInfo, espn_ids: list[int]) -> Optional[DropEvidence]:
    """The roster's samples and projections (ESPN leagues); None when the lookup fails, never a failure."""
    if league_info.provider != FantasyProvider.ESPN or not espn_ids:
        return None
    try:
        return await run_db("ai.drop_evidence", _drop_evidence, league_info, espn_ids)
    except Exception as exc:
        log.warning("ai_drop_evidence_unavailable", error=str(exc))
        return None


# The injury slots, which a healthy add can't take: ESPN's IR, Yahoo's IL and IL+.
INJURY_SLOTS = ("IR", "IL", "IL+")


def _roster_room(team: Any, roster: Sequence[Any]) -> int:
    """Open active roster spots: the league's slots less the injury ones, less the players not in one;
    none when the slots are unknown."""
    slots = _roster_slots(team)
    total = sum(int(n or 0) for name, n in slots.items() if str(name).upper() not in INJURY_SLOTS)
    active = sum(1 for p in roster if (p.lineup_slot or "").upper() not in INJURY_SLOTS)
    return max(0, total - active) if total else 0


async def _availability(league_info: LeagueInfo, espn_ids: list[int]) -> dict[int, PoolEntry]:
    """How the provider's pool sees the adds right now (ESPN); {} when unknown, never a failure."""
    if league_info.provider != FantasyProvider.ESPN or not espn_ids:
        return {}
    try:
        return await EspnService.get_player_pool_entries(league_info, espn_ids)
    except Exception as exc:
        log.warning("ai_add_availability_unavailable", error=str(exc))
        return {}


def _availability_of(entry: Optional[PoolEntry], own_team_id: int) -> Optional[str]:
    if entry is None or entry.status is None:
        return None
    if entry.status == "FREEAGENT":
        return "free agent"
    if entry.status == "WAIVERS":
        return f"on waivers until {entry.waivers_until}" if entry.waivers_until else "on waivers"
    if entry.on_team_id == own_team_id:
        return "on your roster"
    return "on another team's roster"


def _player(p: Optional[SidePlayer]) -> Optional[dict[str, Any]]:
    return None if p is None else {"player_id": p.player_id, "name": p.name, "team": p.team}


# ------------------------------- project_matchup ------------------------------- #


def _counts(p: Any, provider: FantasyProvider) -> bool:
    """Whether the projection counts a roster player: off IR and not injured (the provider builds' rule)."""
    benched = ("IR", "IL", "IL+", "BE") if provider == FantasyProvider.YAHOO else ("IR", "")
    return p.lineup_slot not in benched and not p.injured


def _add_values(scoring: Any, adds: Sequence[_Identity]) -> dict[int, tuple[Optional[float], Optional[str], Optional[StatLine]]]:
    """Each add's per-game value and its source under the league's scoring, and his per-game line
    (last `ADD_LINE_DAYS` days, last season's before the season has them), by NBA id."""
    by_espn = [a for a in adds if a.espn_id is not None]
    by_name = [a for a in adds if a.espn_id is None]
    values: dict[int, tuple[Optional[float], Optional[str]]] = {}
    lines: dict[int, Optional[StatLine]] = {}
    if by_espn:
        ids = [a.espn_id for a in by_espn]
        valued = PlayerValueService.avg_points_for(scoring, espn_ids=ids)
        rolling = PlayerValueService.rolling_lines_by_espn_id(ids, ADD_LINE_DAYS)
        baseline = PlayerValueService.baseline_lines_by_espn_id([e for e in ids if rolling.get(e) is None])
        for a in by_espn:
            v = valued.get(a.espn_id)
            values[a.nba_id] = (v.value, v.source) if v is not None else (None, None)
            lines[a.nba_id] = rolling.get(a.espn_id) or baseline.get(a.espn_id)
    if by_name:
        names = [(a.name, a.team or "") for a in by_name]
        valued = PlayerValueService.avg_points_for(scoring, names=names)
        rolling = PlayerValueService.rolling_lines_by_name(names, ADD_LINE_DAYS)
        baseline = PlayerValueService.baseline_lines_by_name(names)
        for a in by_name:
            key = _normalize_name(a.name)
            v = valued.get(key)
            values[a.nba_id] = (v.value, v.source) if v is not None else (None, None)
            lines[a.nba_id] = rolling.get(key) or baseline.get(key)
    return {pid: (*values[pid], lines.get(pid)) for pid in values}


async def project_matchup(user_id: int, team: Any, league_info: LeagueInfo, args: ProjectMatchupInput,
                          now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    resp = await MatchupService.get_matchup_by_team_id(user_id, team.team_id, args.window)
    md = resp.data
    if md is None:
        return {"team_id": team.team_id, "projection": None, "note": resp.message or "No current matchup"}
    today = schedule_service.get_nba_today()
    week = schedule_service.get_current_matchup(today)
    if week is None:
        return {"team_id": team.team_id, "projection": None, "note": "No matchup on the calendar today"}
    today_index: int = week["current_day_index"]
    last_day = (week["end_date"] - week["start_date"]).days
    first_open = await run_db("ai.first_open_day", _first_open_day, today, today_index, now)
    if first_open > last_day:
        return {"team_id": team.team_id, "projection": None,
                "note": "The matchup has no day left a move made now could count for"}
    provider = league_info.provider
    scoring = await run_db("ai.scoring", PlayerValueService.scoring_for, league_info, team.team_id)
    categories = scoring.categories if md.scoring_format == "categories" and scoring.categories is not None else None

    roster = [SidePlayer(p.nba_player_id, p.name, p.team, float(p.avg_points or 0.0),
                         _game_days(week, p.team, today_index), _counts(p, provider), p._avg_line)
              for p in md.your_team.roster]
    by_nba = {p.player_id: p for p in roster if p.player_id is not None}
    espn_ids = {p.nba_player_id: p.player_id for p in md.your_team.roster if p.nba_player_id is not None} \
        if provider == FantasyProvider.ESPN else {}
    sources = {p.nba_player_id: p._value_source for p in md.your_team.roster if p.nba_player_id is not None}

    # The adds, resolved by NBA id
    add_ids = [m.add for m in args.moves if m.add is not None]
    identities = await run_db("ai.identities", _identities, add_ids) if add_ids else {}
    missing = [pid for pid in add_ids if pid not in identities]
    if missing:
        raise AppError("PLAYER_NOT_FOUND", f"No NBA player with id {missing[0]}")
    valued = await run_db("ai.add_values", _add_values, scoring, list(identities.values())) if identities else {}
    entries = await _availability(league_info, [who.espn_id for who in identities.values() if who.espn_id is not None])
    adds: dict[int, SidePlayer] = {}
    availability: dict[int, Optional[str]] = {}
    assumes: list[str] = [
        "Every counting player (off IR, not injured) starts every game he has, as in the matchup projection.",
    ]
    for pid, who in identities.items():
        value, source, line = valued.get(pid, (None, None, None))
        if who.team is None:
            assumes.append(f"{who.name}'s NBA team is unknown to Court Vision, so he is projected for no games.")
        if value is None:
            assumes.append(f"Court Vision has no per-game value for {who.name}; he is projected at 0 per game.")
        adds[pid] = SidePlayer(pid, who.name, who.team or "", float(value or 0.0),
                               _game_days(week, who.team, today_index), True, line)
        status = _availability_of(entries.get(who.espn_id) if who.espn_id is not None else None, md.your_team.team_id)
        availability[pid] = status
        if status == "on another team's roster":
            assumes.append(f"{who.name} is on another team's roster: this is a what-if, not a move you can make.")
        elif status is not None and status.startswith("on waivers"):
            assumes.append(f"{who.name} is {status}: he can be claimed, not added outright.")
    if adds:
        assumes.append("Added players are assumed healthy and starting; check get_player_status before relying on one.")

    # The moves in day order, the drop chosen where an add has none and the roster is full
    evidence = await _evidence_for(league_info, [espn_ids[pid] for pid in by_nba if pid in espn_ids])
    room = _roster_room(team, md.your_team.roster)
    holdings: dict[int, SidePlayer] = dict(by_nba)
    weighed: dict[int, Optional[DropValue]] = {}
    for pid, p in by_nba.items():
        # `evidence` is keyed by ESPN id; a Yahoo roster has none, and the board's values stand
        weighed[pid] = weigh_drop(espn_ids.get(pid, pid), p.value, sources.get(pid), evidence if pid in espn_ids else None)
    planned: list[tuple[int, MoveInput]] = []
    for m in args.moves:
        day = first_open if m.day is None else m.day
        if day > last_day:
            raise AppError("AI_MOVE_INVALID", f"Day {m.day} is after the matchup's last day ({last_day})")
        if day < first_open:
            assumes.append(f"A move for day {day} counts from day {first_open}, the first a move made now can count for.")
            day = first_open
        planned.append((day, m))
    planned.sort(key=lambda dm: dm[0])
    moves: list[Move] = []
    chosen: dict[int, str] = {}
    for day, m in planned:
        add = adds.get(m.add) if m.add is not None else None
        drop_id = m.drop
        if drop_id is not None and drop_id not in holdings:
            name = identities[drop_id].name if drop_id in identities else f"player {drop_id}"
            raise AppError("AI_MOVE_INVALID", f"{name} is not on the roster on day {day}")
        if add is not None and add.player_id in holdings:
            raise AppError("AI_MOVE_INVALID", f"{add.name} is already on the roster on day {day}")
        if add is not None and drop_id is None:
            if room > 0:
                room -= 1
                assumes.append(f"{add.name} takes an open roster spot on day {day}; nobody is dropped.")
            else:
                candidates = []
                for pid, p in holdings.items():
                    w = weighed.get(pid)
                    candidates.append(DropCandidate(p, w.value if w else None, bool(w and w.projected)))
                choice = choose_drop(candidates, day=day, last_day=last_day)
                if choice is None:
                    raise AppError("AI_MOVE_INVALID", "The roster is full and nobody on it can be dropped by the rule; name the drop")
                drop_id = choice.candidate.player.player_id
                chosen[len(moves)] = (
                    f"the rule's drop: {choice.games_lost} game(s) left at "
                    f"{choice.candidate.weighed:.1f} per game"
                    + (" (his season projection)" if choice.candidate.projected else "")
                )
        if drop_id is not None:
            holdings.pop(drop_id)
            if add is None:
                room += 1
        if add is not None:
            holdings[add.player_id] = add
            weighed[add.player_id] = weigh_drop(add.player_id, add.value, valued.get(add.player_id, (None, None))[1], None)
        moves.append(Move(day, add, drop_id))

    try:
        what_if = apply_moves(roster, moves, last_day=last_day)
    except MoveInvalid as exc:
        raise AppError("AI_MOVE_INVALID", str(exc)) from exc
    before_counted = games_counted(roster, last_day=last_day)

    start = week["start_date"]
    move_rows = []
    for i, applied in enumerate(what_if.moves):
        move_rows.append({
            "day": applied.day,
            "date": start + timedelta(days=applied.day),
            "add": applied.add.name if applied.add else None,
            "add_player_id": applied.add.player_id if applied.add else None,
            "add_team": applied.add.team if applied.add else None,
            "add_games": applied.add_games,
            "availability": availability.get(applied.add.player_id) if applied.add else None,
            "drop": applied.drop.name if applied.drop else None,
            "drop_player_id": applied.drop.player_id if applied.drop else None,
            "drop_team": applied.drop.team if applied.drop else None,
            "drop_games": applied.drop_games,
            "drop_chosen": chosen.get(i),
        })

    out: dict[str, Any] = {
        "team_id": team.team_id,
        "matchup_period": md.matchup_period,
        "start": md.matchup_period_start,
        "end": md.matchup_period_end,
        "today": today_index,
        "first_open_day": first_open,
        "last_day": last_day,
        "scoring": md.scoring_format,
        "window": args.window,
        "moves": move_rows,
    }
    opp_projected = md.opponent_team.projected_score
    if categories is None:
        current = float(md.your_team.current_score or 0.0)
        before = points_projection(current, before_counted)
        after = points_projection(current, what_if.counted)
        out.update({
            "before": {"you": before, "opponent": opp_projected, "margin": round(before - opp_projected, 2)},
            "after": {"you": after, "opponent": opp_projected, "margin": round(after - opp_projected, 2)},
            "change": round(after - before, 2),
            "categories": None,
        })
    else:
        cat = md.your_team.categories
        base = CategoryTeamScoreData(totals=dict(cat.totals) if cat else {}, raw=dict(cat.raw) if cat and cat.raw else None)
        opp_totals = {c.key: c.opp for c in (md.projected_category_comparison.items if md.projected_category_comparison else [])}
        before_totals = category_projection(categories, base, before_counted)
        after_totals = category_projection(categories, base, what_if.counted)
        cmp_before = categories.compare(before_totals, opp_totals)
        cmp_after = categories.compare(after_totals, opp_totals)
        rows = []
        for b, a in zip(cmp_before.items, cmp_after.items):
            rows.append({
                "category": b.key, "label": b.label,
                "projected_you": b.you, "projected_you_after": a.you, "projected_opponent": b.opp,
                "leader": b.winner, "leader_after": a.winner, "flips": a.winner != b.winner,
                "is_rate": b.is_rate, "lower_is_better": not b.higher_is_better,
            })
        out.update({
            "before": {"you": cmp_before.wins, "opponent": cmp_before.losses, "ties": cmp_before.ties},
            "after": {"you": cmp_after.wins, "opponent": cmp_after.losses, "ties": cmp_after.ties},
            "change": cmp_after.wins - cmp_before.wins,
            "categories": rows,
        })
        if any(p.line is None for p, g in before_counted if p.counts and g):
            assumes.append("Some roster players have no per-game line in this read; they add nothing to the category totals.")
    out["assumes"] = assumes
    out["labels"] = LABELS
    return out


# ------------------------------- plan_streams ------------------------------- #


def _is_out(injury_status: Optional[str]) -> bool:
    return (injury_status or "ACTIVE").upper() in OUT_STATUSES


def _rehold_after(acq: Any) -> int:
    """Days before a dropped player can hold a spot again (`stream_lane_service.rehold_after_for`
    without its cap: the joint planner handles any gap)."""
    hours = 24 if acq is None or acq.waiver_hours is None else acq.waiver_hours
    return 1 if hours <= 0 else 1 + math.ceil(hours / 24)


def _category_lines(espn_ids: list[int]) -> dict[int, Optional[StatLine]]:
    rolling = PlayerValueService.rolling_lines_by_espn_id(espn_ids, BOARD_WINDOW_DAYS)
    baseline = PlayerValueService.baseline_lines_by_espn_id([e for e in espn_ids if rolling.get(e) is None])
    return {e: rolling.get(e) or baseline.get(e) for e in espn_ids}


@dataclass(frozen=True)
class _Valued:
    values: Optional[Mapping[int, float]]        # by ESPN id; None = the board's own values
    kind: str
    weights: list[dict[str, Any]]
    notes: list[str]


def _matchup_values(scoring: Any, margins_by_key: Mapping[str, tuple[float, float, bool]], games: float) -> _Valued:
    """Every pool player's category value re-weighted toward the matchup's close categories."""
    cat_defs = [c for c in rankable_categories(scoring) if not c.is_rate and c.key in margins_by_key]
    if not cat_defs:
        return _Valued(None, LEAGUE, [], ["No counting category of this league is in the matchup read; valued as 'league'."])
    _, pool = PlayerValueService._category_pool(MATCHUP_DAYS)
    if not pool:
        return _Valued(None, LEAGUE, [], ["No player pool is loaded yet; valued as 'league'."])
    keys = [c.key for c in cat_defs]
    margins = {k: signed_margin(*margins_by_key[k]) for k in keys}
    weights = matchup_weights(margins, pool_stds(pool, keys), games)
    scored = compute_category_scores(pool, cat_defs)
    values = {s.row.espn_id: weighted_category_value(s.z, weights) for s in scored if s.row.espn_id is not None}
    rows = [{"category": k, "label": next(c.label for c in cat_defs if c.key == k),
             "margin": round(margins[k], 2), "weight": round(weights[k], 3)} for k in keys]
    return _Valued(values, MATCHUP, rows, [])


def _category_values(key: str, espn_ids: list[int]) -> _Valued:
    lines = _category_lines(espn_ids)
    return _Valued({e: round(category_rate(line, key), 2) for e, line in lines.items()}, key, [], [])


async def plan_streams(user_id: int, team: Any, league_info: LeagueInfo, args: PlanStreamsInput,
                       now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    adapter = get_provider_adapter(league_info.provider)
    if not adapter.capabilities(league_info).transactions:
        raise AppError("AI_PROVIDER_UNSUPPORTED",
                       "Stream plans need an ESPN league: Court Vision can't plan adds and drops for this provider yet")
    board = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
    period, base_date = StreamLaneService._board_today(board)
    end_period, end_date = StreamLaneService._end_period(board)
    today = (await run_db("ai.lane_today", _days, period, period, period, base_date))[0]
    start = first_open_period(today, now)
    if start > end_period:
        return {"team_id": team.team_id, "plan": None, "note": "The matchup has no day left a pickup could count for"}
    rehold = _rehold_after(board.acquisitions)
    days = await run_db("ai.lane_days", _days, start, end_period, period, base_date)
    pending = await run_db("ai.lane_pending", _pending_pickups, team.team_id)
    pool = await StreamLaneService._pool(league_info, adapter, team.team_id)
    acq = board.acquisitions
    first_day_adds = None
    if acq is not None and acq.per == "day" and acq.limit is not None and board.espn_team_id is not None:
        try:
            first_day_adds = await adds_for_day(league_info, board.espn_team_id, start)
        except Exception as exc:  # ESPN refuses an over-limit add itself
            log.warning("ai_day_adds_unavailable", team_id=team.team_id, error=str(exc))
    budget = lane_budget(acq, days, pending, first_day_adds=first_day_adds)
    scoring = await run_db("ai.scoring", PlayerValueService.scoring_for, league_info, team.team_id)

    on_board = {p.player_id: p for p in board.players}
    in_pool = {f.player_id: f for f in pool}
    nba_ids = await run_db("ai.nba_ids", nba_ids_by_espn_id, sorted({*on_board, *in_pool}))
    espn_ids = {nba: espn for espn, nba in nba_ids.items()}
    notes: list[str] = []

    # The value function
    if args.value == LEAGUE:
        valued = _Valued(None, LEAGUE, [], [])
    elif args.value == MATCHUP:
        if not scoring.is_categories:
            valued = _Valued(None, LEAGUE, [], ["'matchup' is a categories-league value; this is a points league, so players are valued as 'league'."])
        else:
            md = (await MatchupService.get_matchup_by_team_id(user_id, team.team_id)).data
            items = md.projected_category_comparison.items if md and md.projected_category_comparison else []
            margins = {c.key: (c.you, c.opp, c.higher_is_better) for c in items}
            games = sum(len(d.teams) for d in days) / NBA_TEAMS
            valued = await run_db("ai.matchup_values", _matchup_values, scoring, margins, games)
    else:
        valued = await run_db("ai.category_values", _category_values, args.value, sorted({*on_board, *in_pool}))
    notes.extend(valued.notes)
    values = valued.values

    def value_of(espn_id: int, default: float) -> float:
        return default if values is None else float(values.get(espn_id, 0.0))

    def games_of(p: Any) -> int:
        team_ = normalize_nba_abbrev(p.team) if p.team else None
        return 0 if _is_out(p.injury_status) else sum(1 for d in days if team_ in d.teams)

    # The holders: named, or the drop rule's. A player a scheduled pickup already drops leaves on its day,
    # so his spot is that pickup's to fill, not the plan's.
    leaving = {pk.drop_player_id: pk.nba_date for pk in pending if pk.drop_player_id is not None}
    holders: list[Any] = []
    chosen_by: dict[int, str] = {}
    if args.holders:
        for nba in args.holders:
            eid = espn_ids.get(nba)
            p = on_board.get(eid) if eid is not None else None
            if p is None:
                raise AppError("AI_NOT_ON_ROSTER", f"Player {nba} is not on this roster")
            if p.lineup_slot_id == IR_SLOT_ID:
                raise AppError("AI_NOT_ON_ROSTER", f"{p.name} is on IR; a streamed spot is an active one")
            if p.player_id in leaving:
                raise AppError("AI_NOT_ON_ROSTER", f"{p.name} is already scheduled to be dropped on {leaving[p.player_id]}")
            if p.player_id not in chosen_by:
                holders.append(p)
                chosen_by[p.player_id] = "named"
    else:
        candidates = [p for p in board.players if p.lineup_slot_id != IR_SLOT_ID and p.player_id not in leaving]
        evidence = await _evidence_for(league_info, [p.player_id for p in candidates])
        ranked = []
        for p in candidates:
            if values is None:
                w = weigh_drop(p.player_id, p.avg_points, p.value_source, evidence)
            else:
                sample = evidence.games.get(p.player_id, 0) if evidence is not None else None
                enough = p.value_source == "baseline" or sample is None or sample >= MIN_SAMPLE_GAMES
                w = DropValue(value_of(p.player_id, p.avg_points), False) if p.value_source is not None and enough else None
            if w is None:
                continue
            games = games_of(p)
            ranked.append((round(w.value * games, 4), games, w.value, p.name, p, w))
        ranked.sort(key=lambda r: r[:4])
        for cost, games, value, _name, p, w in ranked[:args.spots]:
            holders.append(p)
            chosen_by[p.player_id] = (f"the drop rule: {games} game(s) left at {value:.1f} per game"
                                      + (" (his season projection)" if w.projected else ""))
        if not holders:
            raise AppError("AI_NO_HOLDER", "Nobody on the roster can be dropped by the rule; name the holders")
        if len(holders) < args.spots:
            notes.append(f"Only {len(holders)} roster player(s) can be dropped by the rule; the plan streams that many spots.")
    holder_ids = [h.player_id for h in holders]

    named: set[int] = set()
    for nba in args.candidates:
        eid = espn_ids.get(nba)
        if eid is None or eid not in in_pool:
            notes.append(f"Player {nba} is not a free agent the plan can take (rostered, out, or unknown).")
        else:
            named.add(eid)
    # The pool the planner sees: the best under the value, plus the candidates named and the players
    # scheduled pickups bring in (the plan's roster-to-be needs their games and positions).
    keep = named | {pk.add_player_id for pk in pending}
    ranked_pool = sorted(pool, key=lambda f: (-value_of(f.player_id, f.value), f.player_id))
    kept = ranked_pool[:POOL_CAP] + [f for f in ranked_pool[POOL_CAP:] if f.player_id in keep]

    spots_plan = await run_cpu("ai.plan_spots", plan_spots, board, holder_ids, kept, days, pending, budget, rehold, values)
    plan = spots_plan.plan

    def who(eid: int) -> dict[str, Any]:
        p = on_board.get(eid) or in_pool.get(eid)
        return {"player_id": nba_ids.get(eid), "name": p.name if p else f"player {eid}", "team": p.team if p else None}

    holder_rows = [{
        **who(h.player_id), "slot": h.lineup_slot, "value_per_game": round(value_of(h.player_id, h.avg_points), 2),
        "games_left": games_of(h), "chosen_by": chosen_by[h.player_id],
    } for h in holders]
    plan_rows = []
    for k, spot in enumerate(plan.spots):
        prev = plan.holder_ids[k]
        for i, (day, pid) in enumerate(zip(days, spot)):
            plan_rows.append({"day": i, "date": day.nba_date, "spot": k + 1, **who(pid),
                              "value": round(spots_plan.values[pid][i], 2), "switch": pid != prev})
            prev = pid
    switch_rows = [{
        "day": s.day, "date": days[s.day].nba_date, "spot": s.spot + 1,
        "drop_player_id": nba_ids.get(s.drop_id), "drop_name": who(s.drop_id)["name"],
        "add_player_id": nba_ids.get(s.add_id), "add_name": who(s.add_id)["name"],
    } for s in plan.switches]
    if not plan.optimal:
        notes.append("The plan is the best found in the time allowed, not a proven optimum.")
    return {
        "team_id": team.team_id,
        "value": args.value,
        "value_kind": valued.kind,
        "value_label": VALUE_LABELS[valued.kind],
        "start": days[0].nba_date,
        "end": end_date,
        "days_left": len(days),
        "adds_left": budget.adds,
        "rehold_after": rehold,
        "holders": holder_rows,
        "plan": plan_rows,
        "switches": switch_rows,
        "projected_value": round(plan.value, 2),
        "hold_value": round(plan.hold_value, 2),
        "gain": round(plan.gain, 2),
        "adds": plan.adds,
        "optimal": plan.optimal,
        "candidates_considered": len(spots_plan.considered),
        "weights": valued.weights or None,
        "notes": notes,
        "labels": LABELS,
    }
