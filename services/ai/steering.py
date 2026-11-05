"""
Dime's steering tools (the Dime autopilot plan, step 4): `steer`, `set_lane` and
`cancel_pickup`, and the chips they make.

Dime is the manager and the automations are its staff: auto-lineup's fill, the
stream lanes and the scheduled pickups do the arithmetic and the timed sends, and
Dime decides what they work with. These tools are how it says so:

- **steer** sets holds (services/steering.py) and clears them: keep a roster
  player, block a free agent, override a player's value on some days (0 when Dime
  expects him to miss), or pin a player where he is for auto-lineup's fill. The
  planners read them as inputs, so a hold shows in the very next plan.
- **set_lane** starts, pauses, resumes or stops a stream lane. A start is planned
  first exactly as the Week desk's preview plans it, and the chip carries the
  gain over holding.
- **cancel_pickup** cancels a pending scheduled pickup, a lane's or the user's.

Each makes a chip, never a change: a proposal in usr.ai_proposals (`kind` steer,
lane or cancel_pickup) the server checked, with what applying it does in
`action`. These chips change no ESPN roster, so they skip the tray: the AI desk
applies one with a plain button through the Week desk's own route for it (the
holds route, the lane routes, the pickup's cancel), the same one-click actions the
Week desk has, with `proposal_id`. The route checks the chip with `for_apply`
(the caller's, this team's, still proposed and unexpired, the same action), does
what the Week desk would, and marks the chip sent. In a check-in the chip is a
shadow proposal like any other: what Dime would have done, never applied.

Whether Dime may apply its own chips is the acting step's (step 5); until then
the user applies every one. The tools, the chips' routes and the pins a sent
lineup chip leaves are behind `DIME_STEERING_ENABLED` (off by default).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.errors import AppError, ConflictError, NotFoundError
from core.logging import get_logger
from db.base import db, db_operation, run_db
from schemas.common import LeagueInfo
from services import schedule_service, steering
from services.ai import proposals
from services.ai.fantasy import _identities
from services.ai.proposals import CANCEL_PICKUP, CHIP_KEY, LANE, PROPOSED, SENT, SHADOW, STEER, SHADOW_WORDS
from services.scheduled_pickup_service import rollover_at

log = get_logger("ai.steering")

STEER_NAME, SET_LANE_NAME, CANCEL_PICKUP_NAME = "steer", "set_lane", "cancel_pickup"
LANE_OPS = ("start", "pause", "resume", "stop")
# What a chip-made lane event says made it: the reason is Dime's, in the event's detail
DIME_REASON = "dime"
LANE_REASONS = {"pause": "paused_by_dime", "stop": "stopped_by_dime", "start": DIME_REASON, "resume": DIME_REASON}
HOLDS_MAX = 8      # holds one chip sets
CLEARS_MAX = 12    # holds one chip clears
REASON_MAX = proposals.REASON_MAX


def _now() -> datetime:
    return datetime.now(timezone.utc)


enabled = steering.enabled


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldInput(_Input):
    kind: Literal["keep", "block", "override", "pin"]
    player_id: int = Field(..., gt=0, lt=2**31)
    start: Optional[date] = None
    until: Optional[date] = None
    share: Optional[float] = Field(None, ge=0, le=steering.SHARE_MAX)

    @model_validator(mode="after")
    def _share_for_override(self) -> "HoldInput":
        if self.kind == steering.OVERRIDE:
            if self.share is None:
                raise ValueError("an override needs share: the part of his usual value to count (0 = he misses)")
            if abs(self.share - 1.0) < 1e-9:
                raise ValueError("share 1 is his usual value: an override at 1 changes nothing")
        elif self.share is not None:
            raise ValueError("share is for an override only")
        if self.start is not None and self.until is not None and self.until < self.start:
            raise ValueError("until is before start")
        return self


class SteerInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    holds: list[HoldInput] = Field(default_factory=list, max_length=HOLDS_MAX)
    clear: list[int] = Field(default_factory=list, max_length=CLEARS_MAX)
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _something(self) -> "SteerInput":
        if not self.holds and not self.clear:
            raise ValueError("set a hold, clear one, or both")
        keys = [(h.kind, h.player_id) for h in self.holds]
        if len(keys) != len(set(keys)):
            raise ValueError("one hold of a kind per player in a chip")
        if len(self.clear) != len(set(self.clear)):
            raise ValueError("a hold is cleared once")
        return self


class SetLaneInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    action: Literal["start", "pause", "resume", "stop"]
    player_id: Optional[int] = Field(None, gt=0, lt=2**31)
    lane_id: Optional[int] = Field(None, gt=0, lt=2**31)
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _names_its_lane(self) -> "SetLaneInput":
        if self.action == "start":
            if self.player_id is None or self.lane_id is not None:
                raise ValueError("start names the roster player whose spot to stream (player_id), not a lane")
        elif self.lane_id is None or self.player_id is not None:
            raise ValueError(f"{self.action} names the lane (lane_id from get_automations), not a player")
        return self


class CancelPickupInput(_Input):
    team_id: int = Field(..., gt=0, lt=2**31)
    pickup_id: int = Field(..., gt=0, lt=2**31)
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)


_REASON = {"type": "string", "maxLength": REASON_MAX,
           "description": "One line on why, with the number behind it; shown on the chip and beside the hold "
                          "or lane in the Week desk"}

STEER_TOOL: dict[str, Any] = {
    "name": STEER_NAME,
    "description": (
        "Steer the automations on one of the user's ESPN teams with holds, as a chip the user applies from "
        "this desk; nothing changes until they do. A hold is one player over a run of days, with your "
        "reason, and ends by its own date: keep (a roster player you won't drop: a stream lane holding his "
        "spot keeps him those days, and your own proposals won't drop him), block (a player you won't add: "
        "no lane picks him up those days, nor will your proposals), override (his value those days as a "
        "share of his usual one, 0 when you expect him to miss, 0.5 for limited minutes: the lanes plan "
        "with it, roster and free agents alike, and at 0 auto-lineup's fill treats him as out), pin "
        "(auto-lineup's fill leaves him in his slot those days). A lineup chip that benches a player pins "
        "him for its day once the user sends it, so the fill won't start him again. clear ends live holds "
        "by hold_id (get_automations lists them with their reasons). The server checks each hold (keep and "
        "pin need a player on the roster, block one who isn't) and refuses what it can't set, saying why. "
        "Days are dates from today on (default today only), at most 28 days from a hold's start. Holds "
        "steer only the automations and your own proposals: the user's own moves are never refused. "
        "Returns the chip with what each hold does; the next plan (get_lineup's fill, plan_streams, a "
        "lane's next decision) reads it once applied."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "holds": {
                "type": "array", "maxItems": HOLDS_MAX,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(steering.KINDS)},
                        "player_id": {"type": "integer", "description": "NBA player_id"},
                        "start": {"type": "string", "format": "date",
                                  "description": "First day (YYYY-MM-DD); default today"},
                        "until": {"type": "string", "format": "date",
                                  "description": "Last day, inclusive (YYYY-MM-DD); default the first day"},
                        "share": {"type": "number", "minimum": 0, "maximum": steering.SHARE_MAX,
                                  "description": "override only: the share of his usual value to count "
                                                 "(0 = he misses)"},
                    },
                    "required": ["kind", "player_id"],
                    "additionalProperties": False,
                },
            },
            "clear": {"type": "array", "items": {"type": "integer"}, "maxItems": CLEARS_MAX,
                      "description": "hold_ids of live holds to end"},
            "reason": _REASON,
        },
        "required": ["team_id", "reason"],
        "additionalProperties": False,
    },
}

SET_LANE_TOOL: dict[str, Any] = {
    "name": SET_LANE_NAME,
    "description": (
        "Start, pause, resume or stop a stream lane on one of the user's ESPN teams, as a chip the user "
        "applies from this desk; nothing changes until they do. start streams the spot of player_id, a "
        "roster player: the server plans it as the Week desk's preview does (with the lanes deciding its "
        "first day beside it, and your holds) and refuses a spot that can't be streamed, saying why; the "
        "chip shows the gain over holding and the adds it would use. pause, resume and stop name an open "
        "lane by lane_id (get_automations lists them): a pause cancels the pickup the lane has scheduled "
        "and stops it deciding until resumed; a stop ends it, keeping the spot's player. To move a lane to "
        "another spot, propose its stop and the new start. Returns the chip."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "action": {"type": "string", "enum": list(LANE_OPS)},
            "player_id": {"type": "integer", "description": "start: NBA player_id of the roster player whose "
                                                            "spot to stream"},
            "lane_id": {"type": "integer", "description": "pause, resume, stop: the lane"},
            "reason": _REASON,
        },
        "required": ["team_id", "action", "reason"],
        "additionalProperties": False,
    },
}

CANCEL_PICKUP_TOOL: dict[str, Any] = {
    "name": CANCEL_PICKUP_NAME,
    "description": (
        "Cancel a pending scheduled pickup on one of the user's ESPN teams, a stream lane's or the user's, "
        "as a chip the user applies from this desk; nothing changes until they do. Name it by pickup_id "
        "(get_scheduled_pickups and get_automations list them). A lane whose pickup is cancelled keeps "
        "its spot's player that day and decides the next day afresh. Returns the chip."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids (an ESPN team)"},
            "pickup_id": {"type": "integer", "description": "The pending pickup"},
            "reason": _REASON,
        },
        "required": ["team_id", "pickup_id", "reason"],
        "additionalProperties": False,
    },
}

STEERING_TOOLS: list[dict[str, Any]] = [STEER_TOOL, SET_LANE_TOOL, CANCEL_PICKUP_TOOL]
STEERING_TOOL_NAMES = frozenset(t["name"] for t in STEERING_TOOLS)


# ------------------------------- words (pure) ------------------------------- #


def _day(d: date) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def effect_of(kind: str, name: str, share: Optional[float]) -> str:
    """What a hold does, in a line, as the chip and the Week desk say it."""
    if kind == steering.KEEP:
        return f"Keep {name}: a lane holding his spot keeps him, and Dime won't drop him"
    if kind == steering.BLOCK:
        return f"Don't add {name}: no lane picks him up, and Dime won't add him"
    if kind == steering.PIN:
        return f"Pin {name}: auto-lineup's fill leaves him in his slot"
    if share is not None and share <= 0:
        return f"Count {name} out: the lanes plan without him, and auto-lineup's fill treats him as out"
    return f"Count {name} at {round((share or 0) * 100)}% of his value in the lanes' plans"


def steer_summary(holds: list[dict[str, Any]], clears: list[dict[str, Any]], shadow: bool) -> str:
    parts = []
    for h in holds:
        verb = {"keep": "Keep", "block": "Block", "pin": "Pin"}.get(h["kind"])
        who = h["player"]["name"]
        when = steering.day_span(date.fromisoformat(h["starts_on"]), date.fromisoformat(h["ends_on"]))
        if verb is None:
            share = h.get("share") or 0
            parts.append(f"{who} {'out' if share <= 0 else f'at {round(share * 100)}%'} {when}")
        else:
            parts.append(f"{verb} {who} {when}")
    if clears:
        parts.append("clear " + ", ".join(f"{c['kind']} on {c['player']['name']}" for c in clears))
    text = "; ".join(parts)
    text = text[:1].upper() + text[1:]
    return f"{text}. {SHADOW_WORDS}" if shadow else f"{text}. Takes effect when you apply it."


def lane_summary(op: str, holder: str, *, end_date: Optional[date] = None, gain: Optional[float] = None,
                 adds: Optional[int] = None, shadow: bool = False) -> str:
    if op == "start":
        text = f"Stream {holder}'s spot" + (f" through {_day(end_date)}" if end_date else "")
        if gain is not None:
            text += f": {gain:+.1f} projected over holding" + (f" with {adds} add(s)" if adds is not None else "")
    else:
        text = {"pause": "Pause", "resume": "Resume", "stop": "Stop"}[op] + f" the lane streaming {holder}'s spot"
    return f"{text}. {SHADOW_WORDS}" if shadow else f"{text}. Done when you apply it."


def pickup_summary(add: str, drop: Optional[str], day: date, lane: bool, shadow: bool) -> str:
    text = f"Cancel the pickup of {add}" + (f" for {drop}" if drop else "") + f" on {_day(day)}"
    if lane:
        text += " (a stream lane's: the lane keeps its spot's player that day)"
    return f"{text}. {SHADOW_WORDS}" if shadow else f"{text}. Cancelled when you apply it."


def _player(espn_id: int, nba_id: Optional[int], name: str, team: Optional[str]) -> dict[str, Any]:
    return {"espn_id": espn_id, "nba_player_id": nba_id, "name": name, "team": team}


def _hold_json(row: Any) -> dict[str, Any]:
    """A live hold as a chip that clears it shows it."""
    share = float(row.share) if row.share is not None else None
    return {"hold_id": row.id, "kind": row.kind,
            "player": _player(row.player_id, row.nba_player_id, row.player_name, row.player_team),
            "share": share, "starts_on": row.starts_on.isoformat(), "ends_on": row.ends_on.isoformat(),
            "effect": effect_of(row.kind, row.player_name, share), "reason": row.reason}


def hold_view(row: Any) -> dict[str, Any]:
    """A live hold as the model reads it (get_automations): NBA ids only."""
    share = float(row.share) if row.share is not None else None
    return {"hold_id": row.id, "kind": row.kind,
            "player": {"player_id": row.nba_player_id, "name": row.player_name, "team": row.player_team},
            "share": share, "from": row.starts_on.isoformat(), "until": row.ends_on.isoformat(),
            "reason": row.reason, "set": row.created_at.date().isoformat() if row.created_at else None}


# ------------------------------- the matchup day ------------------------------- #


def _matchup_day(today: date, d: date) -> int:
    week = schedule_service.get_current_matchup(today)
    if week is None:
        raise AppError("AI_PROPOSAL_INVALID", "No matchup on the calendar today")
    return (d - week["start_date"]).days


# ------------------------------- steer ------------------------------- #


def _pending_pickups(team_id: int) -> list[Any]:
    from db.models.scheduled_pickups import ScheduledPickup as Row
    return list(Row.select().where((Row.team == team_id) & (Row.status == "pending")).order_by(Row.nba_date, Row.id))


async def steer(user_id: int, conversation_id: int, team: Any, league_info: LeagueInfo, args: SteerInput,
                now: Optional[datetime] = None, *, shadow: bool = False) -> dict[str, Any]:
    """Check the holds and store them as one chip; returns the model's view with the frontend's chip
    under `CHIP_KEY`. Raises `AppError` (a readable refusal) for a hold that can't be set."""
    now = now or _now()
    today = schedule_service.get_nba_today()
    state = await proposals._board(team, league_info)     # who is on the roster, by ESPN id
    live = await steering.live_rows(team.team_id, today)
    by_id = {r.id: r for r in live}
    clears = []
    for hid in args.clear:
        row = by_id.get(hid)
        if row is None:
            raise AppError("AI_PROPOSAL_INVALID", f"Hold {hid} is not one of this team's live holds "
                                                  "(get_automations lists them)")
        clears.append(row)

    identities = await run_db("ai.identities", _identities, [h.player_id for h in args.holds])
    missing = [h.player_id for h in args.holds if h.player_id not in identities]
    if missing:
        raise AppError("PLAYER_NOT_FOUND", f"No NBA player with id {missing[0]}")
    on_board = {p.nba_player_id: p for p in state.players if p.nba_player_id is not None}
    by_espn = {p.player_id: p for p in state.players}
    holds: list[dict[str, Any]] = []
    notes: list[str] = []
    for h in args.holds:
        who = identities[h.player_id]
        start = h.start or today
        until = h.until or start
        if start < today:
            raise AppError("AI_PROPOSAL_INVALID", f"A hold starts today ({today.isoformat()}) at the earliest")
        if until < start:
            raise AppError("AI_PROPOSAL_INVALID", f"The hold on {who.name} ends before it starts")
        if (until - start).days > steering.MAX_DAYS:
            raise AppError("AI_PROPOSAL_INVALID", f"A hold lasts at most {steering.MAX_DAYS} days after its start; "
                                                  "set a nearer date and look again then")
        held = on_board.get(h.player_id) or (by_espn.get(who.espn_id) if who.espn_id is not None else None)
        if h.kind in (steering.KEEP, steering.PIN) and held is None:
            raise AppError("AI_PROPOSAL_INVALID", f"{who.name} is not on the roster, so he can't be "
                                                  f"{'kept' if h.kind == steering.KEEP else 'pinned'}")
        if h.kind == steering.BLOCK and held is not None:
            raise AppError("AI_PROPOSAL_INVALID", f"{who.name} is on the roster: block is for a player you won't "
                                                  "add (keep him, or override his value, instead)")
        espn_id = held.player_id if held is not None else who.espn_id
        if espn_id is None:
            raise AppError("AI_PROPOSAL_INVALID", f"Court Vision has no ESPN id for {who.name}, so it can't hold him")
        name = held.name if held is not None else who.name
        nba_team = (held.team if held is not None else None) or who.team
        for row in live:
            if row.kind == h.kind and row.player_id == espn_id and row.id not in args.clear \
                    and row.starts_on <= until and start <= row.ends_on:
                notes.append(f"{name} already has a {row.kind} hold {steering.day_span(row.starts_on, row.ends_on)} "
                             f"(hold {row.id}); clear it in the same chip if this one replaces it.")
        holds.append({"kind": h.kind, "player": _player(espn_id, who.nba_id, name, nba_team),
                      "share": h.share, "starts_on": start.isoformat(), "ends_on": until.isoformat(),
                      "effect": effect_of(h.kind, name, h.share)})

    if len(live) - len(clears) + len(holds) > steering.MAX_LIVE:
        raise AppError("AI_PROPOSAL_INVALID", f"A team carries at most {steering.MAX_LIVE} live holds; clear some "
                                              "that no longer matter first")
    # A hold steers the planners from now on; a pickup already scheduled is made as it stands
    for pk in await run_db("ai.steer_pending", _pending_pickups, team.team_id):
        for h in holds:
            start, until = date.fromisoformat(h["starts_on"]), date.fromisoformat(h["ends_on"])
            if not start <= pk.nba_date <= until:
                continue
            who = h["player"]["name"]
            if h["kind"] == steering.KEEP and pk.drop_player_id == h["player"]["espn_id"]:
                notes.append(f"A pending pickup drops {who} on {_day(pk.nba_date)} (pickup_id {pk.id}): a keep "
                             "doesn't stop a pickup already scheduled, so cancel_pickup it too.")
            if h["kind"] == steering.BLOCK and pk.add_player_id == h["player"]["espn_id"]:
                notes.append(f"A pending pickup adds {who} on {_day(pk.nba_date)} (pickup_id {pk.id}): a block "
                             "doesn't stop a pickup already scheduled, so cancel_pickup it too.")

    first = min((date.fromisoformat(h["starts_on"]) for h in holds), default=today)
    clear_rows = [_hold_json(r) for r in clears]
    period = state.scoring_period_id + (first - date.fromisoformat(state.nba_date)).days
    row = await proposals._insert(
        user_id=user_id, conversation_id=conversation_id, team_id=team.team_id, kind=STEER,
        day=_matchup_day(today, first), scoring_period_id=period, nba_date=first,
        action={"holds": holds, "clear": clear_rows}, reason=args.reason.strip(),
        summary=steer_summary(holds, clear_rows, shadow), projection=None,
        state=SHADOW if shadow else PROPOSED, expires_at=rollover_at(first + timedelta(days=1)),
        created_at=now, updated_at=now,
    )
    log.info("ai_proposal_created", proposal_id=row.id, team_id=team.team_id, kind=STEER, holds=len(holds),
             clears=len(clears), conversation_id=conversation_id, shadow=shadow)
    return {**proposals.view_of(row, now), "notes": notes, CHIP_KEY: proposals.chip_of(row, now)}


# ------------------------------- set_lane ------------------------------- #


async def set_lane(user_id: int, conversation_id: int, team: Any, league_info: LeagueInfo, args: SetLaneInput,
                   now: Optional[datetime] = None, *, shadow: bool = False) -> dict[str, Any]:
    from schemas.stream_lane import StreamLanePlanReq
    from services.stream_lane_service import ACTIVE, PAUSED, StreamLaneService

    now = now or _now()
    today = schedule_service.get_nba_today()
    projection = None
    if args.action == "start":
        who = (await run_db("ai.identities", _identities, [args.player_id])).get(args.player_id)
        if who is None:
            raise AppError("PLAYER_NOT_FOUND", f"No NBA player with id {args.player_id}")
        if who.espn_id is None:
            raise AppError("AI_PROPOSAL_INVALID", f"Court Vision has no ESPN id for {who.name}")
        # The Week desk's own dry run: it refuses a spot that can't be streamed, and plans with the holds
        plan = (await StreamLaneService.plan(team, league_info, StreamLanePlanReq(player_id=who.espn_id))).data
        p = plan.projection
        holder = _player(plan.holder.player_id, plan.holder.nba_player_id or who.nba_id, plan.holder.name,
                         plan.holder.team)
        action = {"op": "start", "lane_id": None, "holder": holder, "gain": p.gain, "adds": p.adds,
                  "value": p.value, "hold_value": p.hold_value, "adds_left": p.adds_left,
                  "first_decision_at": plan.first_decision_at.isoformat(), "end_date": plan.end_date.isoformat(),
                  "days": [{"date": d.nba_date.isoformat(), "value": d.value, "switch": d.switch,
                            "player": _player(d.player.player_id, d.player.nba_player_id, d.player.name,
                                              d.player.team)} for d in p.days]}
        period = plan.start_period
        first = p.days[0].nba_date if p.days else today
        projection = {"change": p.gain, "scoring": "lane"}
        summary = lane_summary("start", holder["name"], end_date=plan.end_date, gain=p.gain, adds=p.adds,
                               shadow=shadow)
    else:
        lanes = (await StreamLaneService.list_for_team(team)).data
        lane = next((ln for ln in (lanes.open if lanes else []) if ln.id == args.lane_id), None)
        if lane is None:
            raise AppError("AI_PROPOSAL_INVALID", f"Lane {args.lane_id} is not one of this team's open lanes "
                                                  "(get_automations lists them)")
        if args.action == "pause" and lane.status != ACTIVE:
            raise AppError("AI_PROPOSAL_INVALID", f"Lane {lane.id} is {lane.status}, so it can't be paused")
        if args.action == "resume" and lane.status != PAUSED:
            raise AppError("AI_PROPOSAL_INVALID", f"Lane {lane.id} is {lane.status}, so it can't be resumed")
        holder = _player(lane.holder.player_id, lane.holder.nba_player_id, lane.holder.name, lane.holder.team)
        action = {"op": args.action, "lane_id": lane.id, "holder": holder, "status": lane.status,
                  "end_date": lane.end_date.isoformat()}
        period = lane.next_period if lane.next_period is not None else lane.end_period
        first = today
        summary = lane_summary(args.action, holder["name"], shadow=shadow)

    row = await proposals._insert(
        user_id=user_id, conversation_id=conversation_id, team_id=team.team_id, kind=LANE,
        day=_matchup_day(today, first), scoring_period_id=period, nba_date=first, action=action,
        reason=args.reason.strip(), summary=summary, projection=projection,
        state=SHADOW if shadow else PROPOSED, expires_at=rollover_at(today + timedelta(days=1)),
        created_at=now, updated_at=now,
    )
    log.info("ai_proposal_created", proposal_id=row.id, team_id=team.team_id, kind=LANE, op=args.action,
             lane_id=args.lane_id, conversation_id=conversation_id, shadow=shadow)
    return {**proposals.view_of(row, now), "notes": [], CHIP_KEY: proposals.chip_of(row, now)}


# ------------------------------- cancel_pickup ------------------------------- #


def _team_pickup(team_id: int, pickup_id: int) -> Optional[Any]:
    from db.models.scheduled_pickups import ScheduledPickup as Row
    return Row.get_or_none((Row.id == pickup_id) & (Row.team == team_id))


async def cancel_pickup(user_id: int, conversation_id: int, team: Any, args: CancelPickupInput,
                        now: Optional[datetime] = None, *, shadow: bool = False) -> dict[str, Any]:
    from services.providers.identity import nba_ids_by_espn_id

    now = now or _now()
    today = schedule_service.get_nba_today()
    pk = await run_db("ai.steer_pickup", _team_pickup, team.team_id, args.pickup_id)
    if pk is None or pk.status != "pending":
        raise AppError("AI_PROPOSAL_INVALID", f"Pickup {args.pickup_id} is not a pending pickup on this team "
                                              "(get_scheduled_pickups lists them)")
    expires = rollover_at(pk.nba_date)
    if expires <= now:
        raise AppError("AI_PROPOSAL_INVALID", "That pickup's day has begun: it is being made or has been")
    ids = [i for i in (pk.add_player_id, pk.drop_player_id) if i]
    nba = await run_db("ai.nba_ids", nba_ids_by_espn_id, ids)
    add = _player(pk.add_player_id, nba.get(pk.add_player_id), pk.add_name, pk.add_team)
    drop = (_player(pk.drop_player_id, nba.get(pk.drop_player_id), pk.drop_name or str(pk.drop_player_id),
                    pk.drop_team) if pk.drop_player_id else None)
    action = {"pickup_id": pk.id, "date": pk.nba_date.isoformat(), "add": add, "drop": drop,
              "lane_id": pk.lane_id}
    row = await proposals._insert(
        user_id=user_id, conversation_id=conversation_id, team_id=team.team_id, kind=CANCEL_PICKUP,
        day=_matchup_day(today, pk.nba_date), scoring_period_id=pk.scoring_period_id, nba_date=pk.nba_date,
        action=action, reason=args.reason.strip(),
        summary=pickup_summary(add["name"], drop["name"] if drop else None, pk.nba_date, pk.lane_id is not None,
                               shadow),
        projection=None, state=SHADOW if shadow else PROPOSED, expires_at=expires, created_at=now, updated_at=now,
    )
    log.info("ai_proposal_created", proposal_id=row.id, team_id=team.team_id, kind=CANCEL_PICKUP,
             pickup_id=pk.id, conversation_id=conversation_id, shadow=shadow)
    return {**proposals.view_of(row, now), "notes": [], CHIP_KEY: proposals.chip_of(row, now)}


# ------------------------------- applying a chip ------------------------------- #


class SteeringOff(ConflictError):
    error_code = "AI_STEERING_OFF"
    default_message = "Dime's steering is switched off, so its chips can't be applied"


async def for_apply(proposal_id: int, user_id: int, team_id: int, kind: str, **match: Any) -> Any:
    """The steering chip a Week desk route says it is applying, checked before anything changes: the
    caller's, this team's, still proposed and unexpired, of `kind`, and the same action (each `match`
    key equal in its `action`). A mismatch is refused, so a chip never does what it doesn't show."""
    if not enabled():
        raise SteeringOff()
    now = _now()
    row = await proposals.get(proposal_id, user_id)
    if proposals.team_of(row) != team_id:
        raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
    state = proposals.state_of(row, now)
    if state != PROPOSED:
        raise ConflictError("AI_PROPOSAL_NOT_PENDING", proposals._NOT_PENDING[state], data={"state": state})
    action = row.action or {}
    same = row.kind == kind and all(_lookup(action, key) == want for key, want in match.items())
    if not same:
        raise AppError("AI_PROPOSAL_MISMATCH", "What is being applied is not what this chip proposes",
                       status_code=422, log_level="warning")
    return row


def _lookup(action: dict[str, Any], key: str) -> Any:
    """`holder.espn_id` style keys into a chip's action."""
    value: Any = action
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


@db_operation("steering.apply_holds")
def _apply_holds(row_id: int, team_id: int, user_id: int, holds: list[dict[str, Any]], clears: list[int],
                 today: date, now: datetime) -> Optional[int]:
    """Mark the chip sent and make its holds, in one transaction: a second apply finds it sent and
    makes nothing. Returns how many holds it set, or None when the chip was no longer proposed."""
    from db.models.ai_conversations import AiProposal
    from db.models.team_holds import TeamHold

    with db.atomic():
        moved = (AiProposal.update(state=SENT, sent_at=now, updated_at=now)
                 .where((AiProposal.id == row_id) & (AiProposal.state == PROPOSED) & (AiProposal.expires_at > now))
                 .execute())
        if not moved:
            return None
        made = 0
        for h in holds:
            start, until = date.fromisoformat(h["starts_on"]), date.fromisoformat(h["ends_on"])
            if until < today:
                continue
            p = h["player"]
            TeamHold.create(team_id=team_id, user_id=user_id, kind=h["kind"], player_id=p["espn_id"],
                            nba_player_id=p.get("nba_player_id"), player_name=p["name"], player_team=p.get("team"),
                            share=h.get("share"), starts_on=max(start, today), ends_on=until,
                            reason=(h.get("reason") or "")[:REASON_MAX] or "Set by Dime",
                            proposal_id=row_id, applied_by="user", created_at=now)
            made += 1
        if clears:
            (TeamHold.update(cleared_at=now, cleared_by="user")
             .where((TeamHold.id.in_(clears)) & (TeamHold.team_id == team_id) & TeamHold.cleared_at.is_null(True))
             .execute())
    return made


async def apply_holds(team: Any, proposal_id: int, now: Optional[datetime] = None) -> int:
    """The holds route's apply: the chip's holds made and its clears done, the chip marked sent. Each
    hold carries the chip's reason."""
    row = await for_apply(proposal_id, team.user_id, team.team_id, STEER)
    now = now or _now()
    action = row.action or {}
    holds = [{**h, "reason": row.reason} for h in action.get("holds") or []]
    clears = [c["hold_id"] for c in action.get("clear") or []]
    made = await _apply_holds(row.id, team.team_id, team.user_id, holds, clears,
                              schedule_service.get_nba_today(), now)
    if made is None:
        raise ConflictError("AI_PROPOSAL_NOT_PENDING", "This chip was already applied or has expired")
    log.info("steering_holds_applied", proposal_id=row.id, team_id=team.team_id, holds=made, clears=len(clears))
    return made


# ------------------------------- the Week desk's list ------------------------------- #


async def desk_holds(team_id: int, message: Optional[str] = None) -> Any:
    """The holds route's answer: the team's live holds, each with what it does and its reason; none
    while steering is off (none steers anything then)."""
    from schemas.common import ApiStatus
    from schemas.team_hold import TeamHold, TeamHoldPlayer, TeamHoldsData, TeamHoldsResp
    on = enabled()
    rows = await steering.live_rows(team_id) if on else []
    holds = []
    for r in rows:
        share = float(r.share) if r.share is not None else None
        holds.append(TeamHold(
            hold_id=r.id, kind=r.kind, share=share, starts_on=r.starts_on, ends_on=r.ends_on, reason=r.reason,
            player=TeamHoldPlayer(espn_id=r.player_id, nba_player_id=r.nba_player_id, name=r.player_name,
                                  team=r.player_team),
            effect=effect_of(r.kind, r.player_name, share), applied_by=r.applied_by,
            proposal_id=r.proposal_id_id if hasattr(r, "proposal_id_id") else r.proposal_id,
            created_at=r.created_at,
        ))
    return TeamHoldsResp(status=ApiStatus.SUCCESS, message=message or f"{len(holds)} hold(s)",
                         data=TeamHoldsData(enabled=on, holds=holds))


# ------------------------------- the chip's check ------------------------------- #


async def problems(row: Any, team: Any) -> list[str]:
    """Why a proposed steering chip can't be applied as it stands; [] when it can."""
    from db.models.stream_lanes import StreamLane
    if not enabled():
        return [SteeringOff.default_message + "."]
    action = row.action or {}
    if row.kind == CANCEL_PICKUP:
        pk = await run_db("ai.steer_pickup", _team_pickup, team.team_id, action.get("pickup_id"))
        return [] if pk is not None and pk.status == "pending" else ["That pickup is no longer pending."]
    if row.kind == LANE and action.get("op") != "start":
        lane = await run_db("ai.steer_lane", lambda lid: StreamLane.get_or_none(
            (StreamLane.id == lid) & (StreamLane.team == team.team_id)), action.get("lane_id"))
        if lane is None or lane.status == "ended":
            return ["That lane has ended."]
        if action.get("op") == "pause" and lane.status != "active":
            return ["That lane is already paused."]
        if action.get("op") == "resume" and lane.status != "paused":
            return ["That lane is already running."]
        return []
    if row.kind == STEER:
        live = {r.id for r in await steering.live_rows(team.team_id)}
        gone = [c for c in action.get("clear") or [] if c["hold_id"] not in live]
        return [f"The {c['kind']} on {c['player']['name']} has already ended." for c in gone]
    return []
