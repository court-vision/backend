"""
Proposals: the agent's `propose_move` and `propose_lineup` tools and the chips
they make (the CV AI plan, § 6, step 5), with `get_lineup`, the board read the
lineup proposals are made from.

A chip is a stored proposal, not a button that sends whatever the model wrote.
The model calls `propose_move` with one roster move on one of the user's ESPN
teams; the server checks it fully before anything is shown -- the team is the
caller's, the add is a free agent in that league now and the drop is on the
roster, the day is in the matchup and not past, the roster has a seat for an add
without a drop, the league's add limits leave room, and the Week desk's own
guards (a scheduled pickup already dropping the player, an active stream lane's
holder, a pickup of the same player already scheduled) -- and stores it in
usr.ai_proposals. A move for today is a **roster transaction**, a move for a
later matchup day a **scheduled pickup**: the same two writes the Week desk
makes, and the same checks (`RosterTransactionService._validate`, the budget).

`propose_lineup` is the third kind, **lineup**: slot moves on one day's board
(start, bench, shift), named one by one or planned by the server's own fill
(`lineup_planner.plan_fill`, what the Week desk's "Optimize today" makes), and
checked with the Week desk's `validate_moves` -- the player is on the board and
in the slot the chip moves him from, unlocked, eligible for the slot, and the
board after the moves fits the league's slot counts. ESPN keeps one lineup per
day, so a lineup chip is for today's board or a later day's, and is written at
once either way through the Week desk's lineup write.

The chip then shows in the AI desk with the agent's one-line reason and the
server's projection of the move. The user stages it into the Week desk's tray
(rendered in the AI desk) and sends it with the slider; the send goes through
the Week desk's routes with the proposal's id (`RosterTransactionReq.proposal_id`,
`SchedulePickupReq.proposal_id`, `ApplyLineupMovesReq.proposal_id`), which mark
it `sent`. Nothing is executed from the AI desk, and the model never learns an
ESPN id: the tool result carries NBA ids, and the chip the frontend gets
(`CHIP_KEY`, popped off the result by the turn loop) carries the ESPN ids the
writes take.

States: `proposed` (stageable), `sent`, `dismissed`; `expired` is read off
`expires_at` -- for a move today the day's first tip-off (or the 3 AM ET turn
into the next day when the day has no games), for a scheduled move the turn into
its day, when ESPN reaches it and scheduling for it closes, for a lineup the turn
into the day after its board's (the board is edited until then; locked players
are the check's business) -- never stored. A refused send leaves the row
proposed for a retry from the tray. A dismiss can't overtake a send: once the
write is with ESPN the move is made, so `mark_sent` wins the race and `dismiss`
only ever moves a row that is still proposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.errors import AppError, ConflictError, NotFoundError
from core.logging import get_logger
from db.base import db_operation, run_db
from db.models.ai_conversations import AiProposal
from db.models.nba.games import Game
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupState
from services import schedule_service
from services.acquisitions import budget_block
from services.ai.fantasy import MoveInput, ProjectMatchupInput, _first_open_day, _identities, project_matchup
from services.daily_actions_service import has_open_seat
from services.lineup_editor_service import _roster_slots, _slot_name, planner_players, slot_counts_of
from services.lineup_planner import ACTIVE_SLOT_IDS, BENCH_SLOT_ID, IR_SLOT_ID, Move, plan_fill, validate_moves
from services.providers import get_provider_adapter
from services.roster_transaction_service import (
    RosterTransactionInvalid,
    RosterTransactionService,
    _lane_holders,
    _scheduled_drop,
)
from services.scheduled_pickup_service import et_at, rollover_at
from utils.espn_helpers import POSITION_MAP

log = get_logger("ai.proposals")

PROPOSE_MOVE_NAME = "propose_move"
PROPOSE_LINEUP_NAME = "propose_lineup"
GET_LINEUP_NAME = "get_lineup"
CHIP_KEY = "chip"
TRANSACTION, SCHEDULED, LINEUP = "transaction", "scheduled", "lineup"
# The slots a lineup move names, as ESPN names them (PG ... UT, BE, IR); 14-15 are never edited
SLOT_IDS: dict[str, int] = {name: sid for sid, name in POSITION_MAP.items()
                            if isinstance(sid, int) and isinstance(name, str) and name and sid <= IR_SLOT_ID}
MOVES_MAX = 30   # ApplyLineupMovesReq.moves
PROPOSED, SENT, DISMISSED, EXPIRED = "proposed", "sent", "dismissed", "expired"
REASON_MAX = 200
# Proposals listed in a turn's header, newest first
HEADER_MAX = 12


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeMoveInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    add: Optional[int] = Field(None, gt=0, description="NBA player_id to add")
    drop: Optional[int] = Field(None, gt=0, description="NBA player_id to drop")
    day: Optional[int] = Field(None, ge=0, le=13)
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _something_moves(self) -> "ProposeMoveInput":
        if self.add is None and self.drop is None:
            raise ValueError("a move needs an add, a drop, or both")
        if self.add is not None and self.add == self.drop:
            raise ValueError("a move can't add and drop the same player")
        return self


PROPOSE_MOVE: dict[str, Any] = {
    "name": PROPOSE_MOVE_NAME,
    "description": (
        "Propose one roster move on one of the user's ESPN teams as a chip the user can stage and "
        "send from this desk: an add, a drop, or an add with a drop, on a matchup day (0 = the "
        "matchup's first day, as project_matchup counts them; default the first day a move made now "
        "counts for). The server checks the move before the chip is shown -- the add is a free agent "
        "in the league now, the drop is on the roster, the day is still open, an add without a drop "
        "has a roster spot, the league's add limits leave room -- and refuses what can't be made, "
        "saying why, so adjust and try again. A move for today goes to ESPN as a roster transaction "
        "when the user sends it; a move for a later day is scheduled and made before that day's "
        "games. `reason` is your one-line case for the move, shown on the chip. Nothing is sent "
        "until the user confirms it, so never say a move was made. Returns the proposal with the "
        "server's projection of what it does to the matchup. Call project_matchup first so the "
        "reason carries a number, and propose each move separately."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "add": {"type": "integer", "description": "NBA player_id to add (a free agent)"},
            "drop": {"type": "integer", "description": "NBA player_id to drop (on the roster)"},
            "day": {"type": "integer", "minimum": 0, "maximum": 13,
                    "description": "The matchup day the move is for (default: the first open day)"},
            "reason": {"type": "string", "maxLength": REASON_MAX,
                       "description": "One line on why, with the number behind it, e.g. "
                                      "'4 games left at 2.4 blocks; puts you ahead in blocks'"},
        },
        "required": ["team_id", "reason"],
        "additionalProperties": False,
    },
}


class LineupMoveInput(_Input):
    player_id: int = Field(..., gt=0, description="NBA player_id of a player on the roster")
    to_slot: str = Field(..., min_length=1, max_length=8, description="The slot to move him to, as ESPN names it")

    @field_validator("to_slot")
    @classmethod
    def _known_slot(cls, value: str) -> str:
        name = value.strip().upper()
        if name not in SLOT_IDS:
            raise ValueError(f"unknown slot {value!r}; one of {', '.join(SLOT_IDS)}")
        return name


class ProposeLineupInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    day: Optional[int] = Field(None, ge=0, le=13)
    moves: Optional[list[LineupMoveInput]] = Field(None, min_length=1, max_length=MOVES_MAX)
    fill: bool = False
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _moves_or_fill(self) -> "ProposeLineupInput":
        if (self.moves is None) == (not self.fill):
            raise ValueError("name the moves, or set fill to have the server plan the day's fill; not both")
        if self.moves is not None:
            ids = [m.player_id for m in self.moves]
            if len(ids) != len(set(ids)):
                raise ValueError("a player may appear in at most one move")
        return self


class GetLineupInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    day: Optional[int] = Field(None, ge=0, le=13)


_DAY_FIELD = {"type": "integer", "minimum": 0, "maximum": 13,
              "description": "The matchup day whose board it is (0 = the matchup's first day, as project_matchup "
                             "counts them; default today). ESPN keeps one lineup per day, and a later day's can "
                             "be set now."}

GET_LINEUP: dict[str, Any] = {
    "name": GET_LINEUP_NAME,
    "description": (
        "The team's lineup board for one day, as ESPN holds it: every player's slot, the slots he is "
        "eligible for, his game that day (or none), whether he is locked (game started), injury and "
        "avg_value; the league's slot counts; and `fill`, the moves the server itself would make to "
        "start every bench player with a game over one without (the Week desk's own planner). Read it "
        "before any lineup question or proposal: it is the only view of eligibility and locks. "
        "Players carry NBA player_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "day": _DAY_FIELD,
        },
        "required": ["team_id"],
        "additionalProperties": False,
    },
}

PROPOSE_LINEUP: dict[str, Any] = {
    "name": PROPOSE_LINEUP_NAME,
    "description": (
        "Propose slot moves on one day's lineup board of one of the user's ESPN teams as a chip the "
        "user can stage and send from this desk: start a bench player, bench a starter, move a player "
        "between slots, IR on or off. Either name the moves (`moves`: a player and the slot he goes to; "
        "a full slot needs its holder moved in the same call, e.g. the starter to BE) or set `fill` "
        "to propose the server's own fill for the day, as get_lineup's `fill` shows it. The server "
        "checks the moves the way the Week desk does -- each player is on the board and unlocked, "
        "eligible for his slot, and the board after them fits the league's slot counts -- and refuses "
        "what can't be sent, saying why, so adjust and try again. The moves go to ESPN as one lineup "
        "write for that day when the user sends the chip, today or a later day alike (nothing is "
        "scheduled). `reason` is your one-line case, shown on the chip. Nothing is sent until the "
        "user confirms it, so never say the lineup was set. Returns the proposal with the moves as the "
        "server resolved them and the day's projected change in a points league. Call get_lineup "
        "first; propose one chip per day."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "day": _DAY_FIELD,
            "moves": {
                "type": "array", "minItems": 1, "maxItems": MOVES_MAX,
                "description": "The moves, each a player and the slot he goes to (his current slot is read "
                               "off the board). Omit with fill.",
                "items": {
                    "type": "object",
                    "properties": {
                        "player_id": {"type": "integer", "description": "NBA player_id, on the roster"},
                        "to_slot": {"type": "string", "enum": list(SLOT_IDS),
                                    "description": "The slot to move him to"},
                    },
                    "required": ["player_id", "to_slot"],
                    "additionalProperties": False,
                },
            },
            "fill": {"type": "boolean",
                     "description": "Propose the server's fill for the day instead of naming moves"},
            "reason": {"type": "string", "maxLength": REASON_MAX,
                       "description": "One line on why, with the number behind it, e.g. "
                                      "'Edwards plays tonight (31.2 avg); Jokic sits idle at UT'"},
        },
        "required": ["team_id", "reason"],
        "additionalProperties": False,
    },
}


# ------------------------------- words and times (pure) ------------------------------- #


def state_of(row: Any, now: datetime) -> str:
    """The chip's state: a proposed row past its expiry reads as expired."""
    if row.state == PROPOSED and _aware(row.expires_at) <= now:
        return EXPIRED
    return row.state


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def dow(d: date) -> str:
    return d.strftime("%a")


def summary_of(kind: str, add_name: Optional[str], drop_name: Optional[str], nba_date: date) -> str:
    """What will happen, in plain words, as the chip says it."""
    if add_name and drop_name:
        move = f"Add {add_name}, drop {drop_name}"
    elif add_name:
        move = f"Add {add_name} into an open roster spot"
    else:
        move = f"Drop {drop_name}"
    if kind == TRANSACTION:
        return f"{move}, today. Goes to ESPN when you send it."
    when = f"{dow(nba_date)} {nba_date.month}/{nba_date.day}"
    return f"{move}, {when}. Scheduled: made for you ahead of {nba_date:%A}'s games."


def role_of(from_slot_id: int, to_slot_id: int) -> str:
    """start: the bench to an active slot; bench: anything to the bench; shift: the rest (active to
    active, IR on or off). The Week desk's `moveRole`, and `LineupEditorService._describe`."""
    if from_slot_id == BENCH_SLOT_ID and to_slot_id in ACTIVE_SLOT_IDS:
        return "start"
    if to_slot_id == BENCH_SLOT_ID:
        return "bench"
    return "shift"


def lineup_words(moves: Sequence[dict[str, Any]]) -> str:
    """The moves in a few words: 'start Anthony Edwards, bench Nikola Jokic, Jalen Green to IR'."""
    parts = [f"start {m['name']}" for m in moves if m["role"] == "start"]
    parts += [f"bench {m['name']}" for m in moves if m["role"] == "bench"]
    parts += [f"{m['name']} to {m['to_slot']}" for m in moves if m["role"] == "shift"]
    return ", ".join(parts)


def lineup_summary(moves: Sequence[dict[str, Any]], nba_date: date, today: bool) -> str:
    """What will happen, as a lineup chip says it. The write is immediate for any day's board."""
    words = lineup_words(moves)
    words = words[:1].upper() + words[1:]
    if today:
        return f"{words}, today. Goes to ESPN when you send it."
    when = f"{dow(nba_date)} {nba_date.month}/{nba_date.day}"
    return f"{words}, {when}. Sets {nba_date:%A}'s lineup on ESPN when you send it."


def lineup_change(state: LineupState, moves: Sequence[Move]) -> Optional[dict[str, Any]]:
    """The day's projected change in fantasy points: what the players entering active slots score
    that day less what those leaving them would have, counting only players with a game who are not
    OUT (the board's `playable`) at the board's `avg_points`. None on a category board, whose value
    is a rank score and not a total, so the chip shows no change there."""
    by_id = {p.player_id: p for p in state.players}
    moved = [by_id[m.player_id] for m in moves if m.player_id in by_id]
    if not moved or any(p.value_kind != "fpts" for p in moved):
        return None
    change = 0.0
    for m in moves:
        p = by_id.get(m.player_id)
        if p is None or not p.playable:
            continue
        was, now = m.from_slot_id in ACTIVE_SLOT_IDS, m.to_slot_id in ACTIVE_SLOT_IDS
        if now and not was:
            change += p.avg_points
        elif was and not now:
            change -= p.avg_points
    return {"scoring": "points", "change": round(change, 1), "basis": "day"}


def expires_at_for(kind: str, nba_date: date, first_tip: Optional[time], now: datetime) -> datetime:
    """When the chip stops being sendable. A move today: the day's first tip-off (after it a move
    counts for the next day, which is not what the chip says), or the rollover into the next day
    (3 AM ET, `rollover_at`, the moment `get_nba_today` and ESPN's own scoring period turn) when the
    day has no games or its tip has passed. A scheduled move: the rollover into its day, when ESPN's
    day begins and a pickup can no longer be scheduled for it (the scheduled pickup route refuses
    the day as `not_future`; the move is then a transaction, which is another chip). A lineup: the
    rollover into the day after its board's, since ESPN's lineup for a day is edited until the day
    turns; players lock one game at a time, which the check reports as it happens."""
    if kind == SCHEDULED:
        return rollover_at(nba_date)
    if kind == LINEUP:
        return rollover_at(nba_date + timedelta(days=1))
    if first_tip is not None:
        tip = et_at(nba_date, first_tip)
        if tip > now:
            return tip
    return rollover_at(nba_date + timedelta(days=1))


def _first_tip(d: date) -> Optional[time]:
    return Game.get_earliest_game_time_on_date(d)


# ------------------------------- shapes ------------------------------- #


def _player_view(nba_id: Optional[int], name: Optional[str], team: Optional[str]) -> Optional[dict[str, Any]]:
    """The model's view of a player: NBA id only."""
    if name is None:
        return None
    return {"player_id": nba_id, "name": name, "team": team}


def _player_chip(espn_id: Optional[int], nba_id: Optional[int], name: Optional[str], team: Optional[str]
                 ) -> Optional[dict[str, Any]]:
    """The frontend's view: the ESPN id the write takes, the NBA id for Scout."""
    if espn_id is None:
        return None
    return {"espn_id": espn_id, "nba_player_id": nba_id, "name": name or str(espn_id), "team": team}


def moves_json(state: LineupState, moves: Sequence[Move]) -> list[dict[str, Any]]:
    """The moves as the row stores them and the chip shows them: the ESPN id the lineup write takes,
    the NBA id for Scout, both slots by id and name, the role and the planner's note."""
    by_id = {p.player_id: p for p in state.players}
    out = []
    for m in moves:
        p = by_id.get(m.player_id)
        out.append({
            "espn_id": m.player_id, "nba_player_id": p.nba_player_id if p else None,
            "name": p.name if p else str(m.player_id), "team": (p.team or None) if p else None,
            "from_slot_id": m.from_slot_id, "from_slot": _slot_name(m.from_slot_id),
            "to_slot_id": m.to_slot_id, "to_slot": _slot_name(m.to_slot_id),
            "role": m.role, "note": m.note,
        })
    return out


def row_moves(row: Any) -> list[Move]:
    """A lineup row's moves, as the planner and the write take them."""
    return [Move(m["espn_id"], m["from_slot_id"], m["to_slot_id"], m.get("role") or "shift", m.get("note"))
            for m in (row.moves or [])]


def _moves_view(row: Any) -> list[dict[str, Any]]:
    """The model's view of a lineup row's moves: NBA ids and slot names only."""
    return [{"player_id": m.get("nba_player_id"), "name": m["name"], "team": m.get("team"),
             "from_slot": m["from_slot"], "to_slot": m["to_slot"], "role": m["role"], "note": m.get("note")}
            for m in (row.moves or [])]


def view_of(row: Any, now: datetime) -> dict[str, Any]:
    """The tool result the model reads (NBA ids only)."""
    return {
        "proposal_id": row.id,
        "state": state_of(row, now),
        "kind": row.kind,
        "day": row.day,
        "date": row.nba_date.isoformat(),
        "add": _player_view(row.add_nba_id, row.add_name, row.add_team) if row.add_player_id is not None else None,
        "drop": _player_view(row.drop_nba_id, row.drop_name, row.drop_team) if row.drop_player_id is not None else None,
        "moves": _moves_view(row),
        "summary": row.summary,
        "reason": row.reason,
        "projection": row.projection,
        "expires_at": _aware(row.expires_at).isoformat(),
    }


def chip_of(row: Any, now: datetime) -> dict[str, Any]:
    """The chip block the frontend renders (an `AiProposalBlock` without its block id)."""
    projection = row.projection or {}
    return {
        "proposal_id": row.id,
        "state": state_of(row, now),
        "kind": row.kind,
        "team_id": row.team_id_id if hasattr(row, "team_id_id") else row.team_id,
        "day": row.day,
        "date": row.nba_date.isoformat(),
        "dow": dow(row.nba_date),
        "scoring_period_id": row.scoring_period_id,
        "add": _player_chip(row.add_player_id, row.add_nba_id, row.add_name, row.add_team),
        "drop": _player_chip(row.drop_player_id, row.drop_nba_id, row.drop_name, row.drop_team),
        "moves": list(row.moves or []),
        "summary": row.summary,
        "reason": row.reason,
        "change": projection.get("change"),
        "scoring": projection.get("scoring"),
        "expires_at": _aware(row.expires_at).isoformat(),
        "sent_at": _aware(row.sent_at).isoformat() if row.sent_at else None,
    }


# ------------------------------- DB ------------------------------- #


@db_operation("ai.proposal_insert")
def _insert(**fields: Any) -> AiProposal:
    return AiProposal.create(**fields)


@db_operation("ai.proposal_get")
def _get(proposal_id: int, user_id: int) -> Optional[AiProposal]:
    return AiProposal.get_or_none(AiProposal.id == proposal_id, AiProposal.user_id == user_id)


@db_operation("ai.proposals_of_conversation")
def _of_conversation(conversation_id: int, user_id: int) -> list[AiProposal]:
    return list(AiProposal.select()
                .where(AiProposal.conversation_id == conversation_id, AiProposal.user_id == user_id)
                .order_by(AiProposal.id))


@db_operation("ai.proposal_dismiss")
def _dismiss(proposal_id: int, user_id: int, now: datetime) -> Optional[AiProposal]:
    """The user's row, moved to dismissed if it is still proposed: one conditional write, so a send
    that marked it in between stands and the row comes back as it then is. None when it isn't theirs."""
    row = AiProposal.get_or_none(AiProposal.id == proposal_id, AiProposal.user_id == user_id)
    if row is None:
        return None
    if row.state == PROPOSED:
        moved = (AiProposal.update(state=DISMISSED, updated_at=now)
                 .where(AiProposal.id == proposal_id, AiProposal.state == PROPOSED).execute())
        if moved:
            row.state = DISMISSED
        else:
            row = AiProposal.get_or_none(AiProposal.id == proposal_id)
    return row


@db_operation("ai.proposal_sent")
def _mark_sent(proposal_id: int, now: datetime, audit_id: Optional[int], pickup_id: Optional[int]) -> bool:
    """Record the send on a row not already sent. A dismiss that landed while the write was with
    ESPN is overtaken: the move was made, and the chip has to say so. A second send never is."""
    return (AiProposal.update(state=SENT, sent_at=now, updated_at=now, audit_id=audit_id, pickup_id=pickup_id)
            .where(AiProposal.id == proposal_id, AiProposal.state != SENT).execute()) > 0


def _pending_pickup_of(team_id: int, add_player_id: int):
    """The team's pending scheduled pickup that already adds this player, or None."""
    from db.models.scheduled_pickups import ScheduledPickup as Row
    return (Row.select()
            .where((Row.team == team_id) & (Row.status == "pending") & (Row.add_player_id == add_player_id))
            .order_by(Row.nba_date, Row.id)
            .first())


# ------------------------------- the checks ------------------------------- #


@dataclass(frozen=True)
class _Who:
    espn_id: int
    nba_id: Optional[int]
    name: str
    team: Optional[str]


def _day_label(d: date) -> str:
    return f"{d:%a %b} {d.day}"


async def _problems(team: Any, league_info: LeagueInfo, state: LineupState, kind: str,
                    add: Optional[_Who], drop: Optional[_Who], nba_date: date) -> list[str]:
    """Why the move can't be made as it stands, in the order the Week desk would find them;
    [] when it can. The roster transaction's own checks first (the pool, the board), then the
    desk's guards, then the roster's room and the league's add limits."""
    problems: list[str] = []
    if not state.can_write:
        problems.append(f"Roster changes are blocked for this team ({state.write_blocked_reason or 'unknown reason'}).")
    if kind == SCHEDULED and nba_date <= date.fromisoformat(state.nba_date):
        # The scheduled pickup route's own first refusal (`not_future`): ESPN's day has reached the
        # chip's; a move for today is a transaction, another chip. The expiry normally gets here first.
        problems.append(f"{_day_label(nba_date)} has begun on ESPN, so a pickup can no longer be scheduled for it; "
                        "ask for the move again.")
    try:
        await RosterTransactionService._validate(league_info, state, add.espn_id if add else None,
                                                 drop.espn_id if drop else None, for_future_day=kind == SCHEDULED)
    except RosterTransactionInvalid as exc:
        problems.append(exc.message)
    if drop is not None:
        if drop.espn_id in await run_db("ai.proposal_lane_holders", _lane_holders, team.team_id):
            problems.append(f"{drop.name} holds the spot a stream lane is streaming; stop the lane to drop him.")
        pickup = await run_db("ai.proposal_scheduled_drop", _scheduled_drop, team.team_id, drop.espn_id)
        if pickup is not None:
            problems.append(f"{drop.name} is already being dropped for {pickup.add_name} on {_day_label(pickup.nba_date)}.")
    if add is not None:
        dup = await run_db("ai.proposal_pending_add", _pending_pickup_of, team.team_id, add.espn_id)
        if dup is not None:
            problems.append(f"A pickup of {add.name} is already scheduled for {_day_label(dup.nba_date)}.")
        if drop is None and not has_open_seat(state):
            problems.append(f"The roster is full: name a player to drop for {add.name} "
                            "(project_matchup picks one by the rule when the move names none).")
        blocked = budget_block(state.acquisitions, nba_date)
        if blocked:
            problems.append(blocked + ".")
    return problems


async def _board(team: Any, league_info: LeagueInfo, scoring_period_id: Optional[int] = None) -> LineupState:
    """Today's board, or a later ESPN day's when `scoring_period_id` names one."""
    adapter = get_provider_adapter(league_info.provider)
    if league_info.provider != FantasyProvider.ESPN or not adapter.capabilities(league_info).transactions:
        raise AppError("AI_PROVIDER_UNSUPPORTED",
                       "Chips need an ESPN team: Court Vision can't make roster changes for this provider yet")
    state = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=_roster_slots(team),
                                      scoring_period_id=scoring_period_id)
    if state.scoring_period_id is None or not state.nba_date:
        raise AppError("AI_PROPOSAL_INVALID", "ESPN reports no current day for this league")
    return state


# ------------------------------- propose_move ------------------------------- #


async def propose_move(user_id: int, conversation_id: int, team: Any, league_info: LeagueInfo,
                       args: ProposeMoveInput, now: Optional[datetime] = None) -> dict[str, Any]:
    """Check the move and store it; returns the model's view of the proposal, with the frontend's
    chip under `CHIP_KEY` for the turn loop to lift off. Raises `AppError` (which the tool runner
    hands the model as a readable refusal) for a move that can't be made."""
    now = now or datetime.now(timezone.utc)
    state = await _board(team, league_info)

    # The day, as project_matchup counts it
    today = schedule_service.get_nba_today()
    week = schedule_service.get_current_matchup(today)
    if week is None:
        raise AppError("AI_PROPOSAL_INVALID", "No matchup on the calendar today")
    today_index: int = week["current_day_index"]
    start: date = week["start_date"]
    last_day = (week["end_date"] - start).days
    first_open = await run_db("ai.first_open_day", _first_open_day, today, today_index, now)
    if first_open > last_day:
        raise AppError("AI_PROPOSAL_INVALID", "The matchup has no day left a move made now could count for")
    notes: list[str] = []
    day = first_open if args.day is None else args.day
    if day > last_day:
        raise AppError("AI_PROPOSAL_INVALID", f"Day {args.day} is after the matchup's last day ({last_day})")
    if day < first_open:
        notes.append(f"Day {day} is no longer open; the move is proposed for day {first_open}, the first a move "
                     "made now counts for.")
        day = first_open
    kind = TRANSACTION if day == today_index else SCHEDULED
    if kind == SCHEDULED and args.add is None:
        # The scheduled pickup is an add (with or without a drop); a drop alone is made now or not at all
        raise AppError("AI_PROPOSAL_INVALID", f"A drop on its own can only be made today (day {today_index}); "
                                              "for a later day name the player to add as well")
    nba_date = start + timedelta(days=day)
    scoring_period_id = state.scoring_period_id + (nba_date - date.fromisoformat(state.nba_date)).days

    # Who: NBA ids in, ESPN ids resolved here and never shown to the model
    wanted = [pid for pid in (args.add, args.drop) if pid is not None]
    identities = await run_db("ai.identities", _identities, wanted)
    missing = [pid for pid in wanted if pid not in identities]
    if missing:
        raise AppError("PLAYER_NOT_FOUND", f"No NBA player with id {missing[0]}")
    on_board = {p.nba_player_id: p for p in state.players if p.nba_player_id is not None}
    by_espn = {p.player_id: p for p in state.players}
    add: Optional[_Who] = None
    drop: Optional[_Who] = None
    if args.add is not None:
        who = identities[args.add]
        if who.espn_id is None:
            raise AppError("AI_PROPOSAL_INVALID", f"Court Vision has no ESPN id for {who.name}, so he can't be added")
        add = _Who(who.espn_id, who.nba_id, who.name, who.team)
    if args.drop is not None:
        who = identities[args.drop]
        held = on_board.get(args.drop) or (by_espn.get(who.espn_id) if who.espn_id is not None else None)
        if held is None:
            raise AppError("AI_PROPOSAL_INVALID", f"{who.name} is not on the roster")
        drop = _Who(held.player_id, who.nba_id, held.name, held.team or who.team)

    problems = await _problems(team, league_info, state, kind, add, drop, nba_date)
    if problems:
        raise AppError("AI_PROPOSAL_INVALID", problems[0])

    # The server's own projection of the move; a failure here is a note, never a refusal
    projection: Optional[dict[str, Any]] = None
    try:
        out = await project_matchup(user_id, team, league_info,
                                    ProjectMatchupInput(team_id=team.team_id,
                                                        moves=[MoveInput(add=args.add, drop=args.drop, day=day)]), now)
        if out.get("before") is not None:
            projection = {"scoring": out["scoring"], "before": out["before"], "after": out["after"],
                          "change": out["change"]}
    except Exception as exc:
        log.warning("ai_proposal_projection_unavailable", team_id=team.team_id, error=str(exc))
        notes.append("The matchup projection for this move is unavailable; the chip shows no change.")

    expires = await run_db("ai.proposal_expiry", lambda d: expires_at_for(kind, d, _first_tip(d), now), nba_date)
    row = await _insert(
        user_id=user_id, conversation_id=conversation_id, team_id=team.team_id, kind=kind, day=day,
        scoring_period_id=scoring_period_id, nba_date=nba_date,
        add_player_id=add.espn_id if add else None, add_nba_id=add.nba_id if add else None,
        add_name=add.name if add else None, add_team=add.team if add else None,
        drop_player_id=drop.espn_id if drop else None, drop_nba_id=drop.nba_id if drop else None,
        drop_name=drop.name if drop else None, drop_team=drop.team if drop else None,
        reason=args.reason.strip(), summary=summary_of(kind, add.name if add else None, drop.name if drop else None, nba_date),
        projection=projection, state=PROPOSED, expires_at=expires, created_at=now, updated_at=now,
    )
    log.info("ai_proposal_created", proposal_id=row.id, team_id=team.team_id, kind=kind, day=day,
             add=add.espn_id if add else None, drop=drop.espn_id if drop else None, conversation_id=conversation_id)
    return {**view_of(row, now), "notes": notes, CHIP_KEY: chip_of(row, now)}


# ------------------------------- get_lineup and propose_lineup ------------------------------- #


@dataclass(frozen=True)
class _DayBoard:
    index: int                 # the matchup day, as the tools count it
    today_index: int
    nba_date: date
    scoring_period_id: int
    state: LineupState


async def _day_board(team: Any, league_info: LeagueInfo, day: Optional[int]) -> _DayBoard:
    """The board a lineup read or proposal is for: today's, or a later matchup day's (ESPN keeps one
    lineup per day, and a later day's is set now). Today's board is read first, for ESPN's day; a
    later day's period is counted from it, as the Week desk's day tabs do."""
    state = await _board(team, league_info)
    today = schedule_service.get_nba_today()
    week = schedule_service.get_current_matchup(today)
    if week is None:
        raise AppError("AI_PROPOSAL_INVALID", "No matchup on the calendar today")
    today_index: int = week["current_day_index"]
    start: date = week["start_date"]
    last_day = (week["end_date"] - start).days
    index = today_index if day is None else day
    if index > last_day:
        raise AppError("AI_PROPOSAL_INVALID", f"Day {day} is after the matchup's last day ({last_day})")
    if index < today_index:
        raise AppError("AI_PROPOSAL_INVALID", f"Day {day} has passed; today is day {today_index}, and a past "
                                              "day's lineup can't be changed")
    nba_date = start + timedelta(days=index)
    board_date = date.fromisoformat(state.nba_date)
    period = state.scoring_period_id + (nba_date - board_date).days
    if nba_date != board_date:
        state = await _board(team, league_info, scoring_period_id=period)
    return _DayBoard(index, today_index, nba_date, period, state)


def _move_view(m: Move, names: dict[int, Any]) -> dict[str, Any]:
    p = names.get(m.player_id)
    return {"player_id": p.nba_player_id if p else None, "name": p.name if p else str(m.player_id),
            "from_slot": _slot_name(m.from_slot_id), "to_slot": _slot_name(m.to_slot_id), "role": m.role, "note": m.note}


def lineup_view(board: _DayBoard, team_id: int) -> dict[str, Any]:
    """The model's view of a day's board (NBA ids only): slots, every player's slot, eligibility,
    game, lock and value, and the server's own fill for the day."""
    state = board.state
    planners = {pp.player_id: pp for pp in planner_players(state)}
    plan = plan_fill(list(planners.values()), slot_counts_of(state))
    by_id = {p.player_id: p for p in state.players}
    players = []
    for p in state.players:
        pp = planners[p.player_id]
        eligible = [_slot_name(sid) for sid in sorted(p.eligible_slot_ids) if sid in ACTIVE_SLOT_IDS] + ["BE"]
        if pp.ir_eligible:
            eligible.append("IR")
        players.append({
            "player_id": p.nba_player_id, "name": p.name, "team": p.team or None, "slot": p.lineup_slot,
            "eligible_slots": eligible, "game": pp.game_note if p.has_game_today else None,
            "locked": p.locked, "injury": p.injury_status if (p.injured or p.injury_status) else None,
            "avg_value": p.avg_points, "value_kind": p.value_kind,
        })
    slots = [{"slot": sd.slot, "count": sd.count} for sd in state.slots] or \
        [{"slot": _slot_name(sid), "count": n} for sid, n in sorted(slot_counts_of(state).items())]
    return {
        "team_id": team_id, "day": board.index, "date": board.nba_date.isoformat(),
        "today": board.index == board.today_index, "scoring_period_id": board.scoring_period_id,
        "can_write": state.can_write, "write_blocked_reason": state.write_blocked_reason,
        "slots": slots, "players": players,
        "fill": {"moves": [_move_view(m, by_id) for m in plan.moves],
                 "unfilled": [{"player_id": by_id[u.player_id].nba_player_id if u.player_id in by_id else None,
                               "name": u.name, "reason": u.reason} for u in plan.unfilled],
                 "summary": plan.summary},
        "notes": ["Players without a player_id have no NBA id in Court Vision and can't be moved from here."]
        if any(p["player_id"] is None for p in players) else [],
    }


async def get_lineup(team: Any, league_info: LeagueInfo, args: GetLineupInput) -> dict[str, Any]:
    return lineup_view(await _day_board(team, league_info, args.day), team.team_id)


def _resolve_moves(state: LineupState, wanted: Sequence[LineupMoveInput]) -> list[Move]:
    """The model's moves (NBA ids, slot names) as the board's (ESPN ids, slot ids, the slot each
    player is in now). Refuses a player who is not on the board."""
    by_nba = {p.nba_player_id: p for p in state.players if p.nba_player_id is not None}
    planners = {pp.player_id: pp for pp in planner_players(state)}
    moves: list[Move] = []
    for w in wanted:
        p = by_nba.get(w.player_id)
        if p is None:
            raise AppError("AI_PROPOSAL_INVALID", f"No player with id {w.player_id} is on this roster "
                                                  "(get_lineup lists the board)")
        to = SLOT_IDS[w.to_slot]
        role = role_of(p.lineup_slot_id, to)
        pp = planners[p.player_id]
        note = pp.game_note if role == "start" else pp.bench_note if role == "bench" and not pp.playable else None
        moves.append(Move(p.player_id, p.lineup_slot_id, to, role=role, note=note))
    return moves


def _lineup_problems(state: LineupState, moves: Sequence[Move]) -> list[str]:
    """Why the moves can't be sent as they stand, as the Week desk's own check would say; [] when
    they can. The check reads every move, so a chip with two problems names both."""
    problems: list[str] = []
    if not state.can_write:
        problems.append(f"Roster changes are blocked for this team ({state.write_blocked_reason or 'unknown reason'}).")
    problems += [e.message for e in validate_moves(planner_players(state), slot_counts_of(state), list(moves))]
    return problems


async def propose_lineup(user_id: int, conversation_id: int, team: Any, league_info: LeagueInfo,
                         args: ProposeLineupInput, now: Optional[datetime] = None) -> dict[str, Any]:
    """Check the slot moves against the day's board and store them as one chip; returns the model's
    view with the frontend's chip under `CHIP_KEY`. Raises `AppError` (a readable refusal for the
    model) for moves the Week desk would refuse."""
    now = now or datetime.now(timezone.utc)
    board = await _day_board(team, league_info, args.day)
    state = board.state
    if args.fill:
        plan = plan_fill(planner_players(state), slot_counts_of(state))
        if plan.is_noop:
            raise AppError("AI_PROPOSAL_INVALID", f"{plan.summary} ({_day_label(board.nba_date)})")
        moves = list(plan.moves)
    else:
        moves = _resolve_moves(state, args.moves or [])
    problems = _lineup_problems(state, moves)
    if problems:
        raise AppError("AI_PROPOSAL_INVALID", " ".join(problems[:3]))

    today = board.index == board.today_index
    stored = moves_json(state, moves)
    row = await _insert(
        user_id=user_id, conversation_id=conversation_id, team_id=team.team_id, kind=LINEUP, day=board.index,
        scoring_period_id=board.scoring_period_id, nba_date=board.nba_date, moves=stored,
        reason=args.reason.strip(), summary=lineup_summary(stored, board.nba_date, today),
        projection=lineup_change(state, moves), state=PROPOSED,
        expires_at=expires_at_for(LINEUP, board.nba_date, None, now), created_at=now, updated_at=now,
    )
    log.info("ai_proposal_created", proposal_id=row.id, team_id=team.team_id, kind=LINEUP, day=board.index,
             moves=len(moves), fill=args.fill, conversation_id=conversation_id)
    return {**view_of(row, now), "notes": [], CHIP_KEY: chip_of(row, now)}


# ------------------------------- the desk's reads and writes ------------------------------- #


async def _owned(proposal_id: int, user_id: int) -> AiProposal:
    row = await _get(proposal_id, user_id)
    if row is None:
        raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
    return row


async def check(proposal_id: int, user_id: int, team: Any, league_info: LeagueInfo,
                now: Optional[datetime] = None) -> tuple[dict[str, Any], list[str]]:
    """The chip as it stands and a fresh check against the board and the pool: what staging it
    reads before the tray takes it. A chip no longer proposed has that as its one problem."""
    now = now or datetime.now(timezone.utc)
    row = await _owned(proposal_id, user_id)
    state = state_of(row, now)
    if state != PROPOSED:
        words = {SENT: "This move was already sent.", DISMISSED: "This chip was dismissed.",
                 EXPIRED: "This chip expired: the day it was for has started."}
        return chip_of(row, now), [words[state]]
    board = await _board(team, league_info)
    if row.kind == LINEUP:
        # The chip's own day's board: today's, or a later day's read as the Week desk's day tab does
        if board.scoring_period_id is not None and row.scoring_period_id > board.scoring_period_id:
            board = await _board(team, league_info, scoring_period_id=row.scoring_period_id)
        return chip_of(row, now), _lineup_problems(board, row_moves(row))
    add = _Who(row.add_player_id, row.add_nba_id, row.add_name, row.add_team) if row.add_player_id else None
    drop = _Who(row.drop_player_id, row.drop_nba_id, row.drop_name, row.drop_team) if row.drop_player_id else None
    return chip_of(row, now), await _problems(team, league_info, board, row.kind, add, drop, row.nba_date)


def team_of(row: Any) -> int:
    return row.team_id_id if hasattr(row, "team_id_id") else row.team_id


async def get(proposal_id: int, user_id: int) -> AiProposal:
    return await _owned(proposal_id, user_id)


async def dismiss(proposal_id: int, user_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Let a proposed chip go. A sent one can't be dismissed (409)."""
    now = now or datetime.now(timezone.utc)
    row = await _dismiss(proposal_id, user_id, now)
    if row is None:
        raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
    if row.state == SENT:
        raise ConflictError("AI_PROPOSAL_SENT", "This move was already sent")
    return chip_of(row, now)


async def for_send(proposal_id: int, user_id: int, team_id: int, *, kind: str, add: Optional[int] = None,
                   drop: Optional[int] = None, scoring_period_id: Optional[int] = None,
                   moves: Optional[Sequence[tuple[int, int, int]]] = None,
                   now: Optional[datetime] = None) -> AiProposal:
    """The proposal a Week desk write says it is sending, checked before the write: the caller's,
    this team's, still proposed and unexpired, and the same move (ESPN ids, kind, day; for a lineup
    the same (player, from, to) slot moves on the same day's board) the request carries. A mismatch
    is refused so a chip can never send a different move than it shows.
    The state is read, not claimed: a dismiss that lands while the write is with ESPN is overtaken
    by `mark_sent` (the move is made either way), and a second send of the same chip is stopped by
    the write itself (the board's version, or the pickup already pending)."""
    now = now or datetime.now(timezone.utc)
    row = await _owned(proposal_id, user_id)
    if team_of(row) != team_id:
        raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
    state = state_of(row, now)
    if state != PROPOSED:
        raise ConflictError("AI_PROPOSAL_NOT_PENDING",
                            {SENT: "This move was already sent", DISMISSED: "This chip was dismissed",
                             EXPIRED: "This chip expired: the day it was for has started"}[state],
                            data={"state": state})
    if kind == LINEUP:
        same = (row.kind == LINEUP and row.scoring_period_id == scoring_period_id
                and sorted(moves or []) == sorted((m.player_id, m.from_slot_id, m.to_slot_id) for m in row_moves(row)))
    else:
        same = (row.kind == kind and row.add_player_id == add and row.drop_player_id == drop
                and (scoring_period_id is None or row.scoring_period_id == scoring_period_id))
    if not same:
        raise AppError("AI_PROPOSAL_MISMATCH", "The move being sent is not the one this chip proposes",
                       status_code=422, log_level="warning")
    return row


async def mark_sent(proposal_id: int, *, audit_id: Optional[int] = None, pickup_id: Optional[int] = None,
                    now: Optional[datetime] = None) -> None:
    """Record that the Week desk's write of this proposal went through. Never raises: the write
    is done, and a chip left proposed is the lesser harm."""
    try:
        if not await _mark_sent(proposal_id, now or datetime.now(timezone.utc), audit_id, pickup_id):
            log.warning("ai_proposal_not_marked_sent", proposal_id=proposal_id)
    except Exception:
        log.exception("ai_proposal_mark_sent_failed", proposal_id=proposal_id)


def _is_chip(part: dict[str, Any]) -> bool:
    return part.get("kind") == "block" and (part.get("block") or {}).get("type") == "proposal"


def _refresh(parts: list[dict[str, Any]], rows: dict[int, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for part in parts:
        row = rows.get(part["block"].get("proposal_id")) if _is_chip(part) else None
        if row is None:
            out.append(part)
        else:
            out.append({**part, "block": {**part["block"], **chip_of(row, now), "id": part["block"].get("id")}})
    return out


async def refresh_history(history: list[list[dict[str, Any]]], conversation_id: int, user_id: int,
                          now: Optional[datetime] = None) -> list[list[dict[str, Any]]]:
    """A conversation's stored turns, each turn's parts with its chips as they stand now: the
    parts were written when the chips were made, and the chips have moved on since (sent,
    dismissed, expired). One read of the conversation's proposals covers every turn."""
    if not any(_is_chip(p) for parts in history for p in parts):
        return history
    now = now or datetime.now(timezone.utc)
    rows = {row.id: row for row in await _of_conversation(conversation_id, user_id)}
    return [_refresh(parts, rows, now) for parts in history]


async def header_line(conversation_id: int, user_id: int, now: Optional[datetime] = None) -> str:
    """What became of this conversation's proposals, for the turn header, so the agent builds on
    the moves the user sent and doesn't propose a dismissed one again. Empty when there are none."""
    rows = await _of_conversation(conversation_id, user_id)
    if not rows:
        return ""
    now = now or datetime.now(timezone.utc)
    items = []
    for row in rows[-HEADER_MAX:]:
        if row.kind == LINEUP:
            who = f"lineup: {lineup_words(row.moves or [])}"
        else:
            who = " / ".join(filter(None, [f"add {row.add_name}" if row.add_name else "", f"drop {row.drop_name}" if row.drop_name else ""]))
        items.append(f"#{row.id} {who} ({dow(row.nba_date)} {row.nba_date.isoformat()}): {state_of(row, now)}")
    return "Your earlier proposals in this conversation: " + "; ".join(items) + "."
