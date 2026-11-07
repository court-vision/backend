"""
Holds: Dime's steering inputs into the automations (usr.team_holds, migration 0043;
the Dime autopilot plan, step 4, "Steering the tools, not replacing them").

Dime manages through the planners rather than around them: what it judges enters
the arithmetic they already do as inputs, so no planner learns anything new about
fantasy basketball and every hold shows up in the next plan.

    keep      a roster player stays: a stream lane holding his spot keeps him
              through the hold's last day (`Streamer.kept`), and Dime's proposals
              won't drop him
    block     a player isn't added: no lane adds him on those days
              (`Streamer.no_add`), and Dime's proposals won't add him
    override  his value on those days is a share of his usual one: the lanes plan
              with it (roster and pool alike), and at share 0, Dime expecting him
              to miss, auto-lineup's fill treats him as out that day
    pin       auto-lineup's fill leaves him where he is on those days: to the
              planner he is locked, so nobody moves him or moves through his slot.
              A lineup chip that benches a player pins him for its day when it is
              sent (`pin_benched`), so the fill doesn't start him again

A hold is read only through its last day (`live_holds`), so none outlives its
date and nothing sweeps them. Holds steer the automations and Dime's proposals,
never the user: "Optimize today" plans with them (it is the fill), but the desk's
own moves are never refused for one, and the user clears any from the Week desk.

The rules (`steer_fill`, `lane_steer`, `blocking`) are pure and table-tested; the
reads fail soft, since a missing hold must never stop auto-lineup or a lane. All of
it is behind `DIME_STEERING_ENABLED` (`enabled`, off by default): switched off, the
planners read no holds at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.logging import get_logger
from db.base import db_operation, run_db
from services.lineup_planner import Plan, PlannerPlayer, plan_fill

log = get_logger("steering")

KEEP, BLOCK, OVERRIDE, PIN = "keep", "block", "override", "pin"
KINDS = (KEEP, BLOCK, OVERRIDE, PIN)
# How far ahead a hold may reach: four weeks from its first day (the table's check)
MAX_DAYS = 28
# Live holds a team may carry at once: Dime merges and clears rather than piling them up
MAX_LIVE = 30
SHARE_MAX = 2.0
REASON_MAX = 200


@dataclass(frozen=True)
class Hold:
    id: int
    kind: str
    player_id: int                  # ESPN
    starts_on: date
    ends_on: date
    share: Optional[float] = None   # override only
    reason: str = ""
    name: str = ""

    def covers(self, day: date) -> bool:
        return self.starts_on <= day <= self.ends_on


def hold_of(row: Any) -> Hold:
    return Hold(id=row.id, kind=row.kind, player_id=row.player_id, starts_on=row.starts_on, ends_on=row.ends_on,
                share=float(row.share) if row.share is not None else None, reason=row.reason,
                name=row.player_name)


# ------------------------------- rules (pure) ------------------------------- #


def on_day(holds: Sequence[Hold], day: date, kind: Optional[str] = None) -> list[Hold]:
    return [h for h in holds if h.covers(day) and (kind is None or h.kind == kind)]


def share_on(holds: Sequence[Hold], player_id: int, day: date) -> float:
    """The share of his value a player counts for on `day`: an override's, or 1. Two overrides on
    one day (Dime set a second without clearing the first) count the one set last (the higher id),
    whichever starts first: the live list is ordered by start day, not by when a hold was set."""
    found = [h for h in holds if h.kind == OVERRIDE and h.player_id == player_id and h.covers(day)]
    return max(found, key=lambda h: h.id).share if found else 1.0


def steer_fill(players: Sequence[PlannerPlayer], holds: Sequence[Hold], day: date) -> list[PlannerPlayer]:
    """The board as auto-lineup's fill should see it on `day`: a pinned player is locked (he
    neither moves nor makes way, and nobody shifts through his slot), and a player Dime expects to
    miss (an override at share 0) is out, so a bench player with a game may take his slot. The
    rest are as ESPN has them."""
    pinned = {h.player_id for h in on_day(holds, day, PIN)}
    out: list[PlannerPlayer] = []
    for p in players:
        if p.player_id in pinned:
            p = replace(p, locked=True)
        elif p.has_game and not p.is_out and share_on(holds, p.player_id, day) <= 0:
            p = replace(p, injury_status="OUT")
        out.append(p)
    return out


def fill_with(players: Sequence[PlannerPlayer], slot_counts: Mapping[int, int], holds: Sequence[Hold],
              day: Optional[date]) -> Plan:
    """The fill on `day` with the team's holds: `plan_fill` over `steer_fill`'s board."""
    if day is None or not holds:
        return plan_fill(players, slot_counts)
    return plan_fill(steer_fill(players, holds, day), slot_counts)


@dataclass(frozen=True)
class LaneSteer:
    """The holds as the lanes' planner takes them, over a horizon of days (index 0 = the first day
    planned): per player, the share of his value each day (only players with an override); the days
    each player may not be added (blocks); and the days each may not leave a spot (keeps)."""

    shares: Mapping[int, tuple[float, ...]]
    no_add: Mapping[int, frozenset[int]]
    kept: Mapping[int, frozenset[int]]

    @property
    def empty(self) -> bool:
        return not (self.shares or self.no_add or self.kept)

    def share(self, player_id: int, index: int) -> float:
        days = self.shares.get(player_id)
        return days[index] if days is not None else 1.0


NO_STEER = LaneSteer({}, {}, {})


def lane_steer(holds: Sequence[Hold], days: Sequence[date]) -> LaneSteer:
    """The holds over `days`. Pins are the fill's business, not the lanes'."""
    if not holds or not days:
        return NO_STEER
    shares: dict[int, tuple[float, ...]] = {}
    for pid in {h.player_id for h in holds if h.kind == OVERRIDE}:
        row = tuple(share_on(holds, pid, d) for d in days)
        if any(s != 1.0 for s in row):
            shares[pid] = row

    def indexes(kind: str) -> dict[int, frozenset[int]]:
        out: dict[int, set[int]] = {}
        for h in holds:
            if h.kind == kind:
                hit = {i for i, d in enumerate(days) if h.covers(d)}
                if hit:
                    out.setdefault(h.player_id, set()).update(hit)
        return {pid: frozenset(ix) for pid, ix in out.items()}

    return LaneSteer(shares, indexes(BLOCK), indexes(KEEP))


def blocking(holds: Sequence[Hold], *, add: Optional[int], drop: Optional[int], day: date) -> Optional[Hold]:
    """The hold a proposed move runs into on `day`: a block on its add, or a keep on its drop."""
    for h in holds:
        if not h.covers(day):
            continue
        if (h.kind == BLOCK and add is not None and h.player_id == add) or \
                (h.kind == KEEP and drop is not None and h.player_id == drop):
            return h
    return None


def kind_words(kind: str, share: Optional[float] = None) -> str:
    if kind == OVERRIDE:
        if share is not None and share <= 0:
            return "expected to miss"
        return f"counted at {round((share or 0) * 100)}% of his value"
    return {KEEP: "kept", BLOCK: "not added", PIN: "pinned in his slot"}[kind]


def day_span(starts_on: date, ends_on: date) -> str:
    """'Thu 11/6' or 'Thu 11/6 to Sat 11/8'."""
    def one(d: date) -> str:
        return f"{d:%a} {d.month}/{d.day}"
    return one(starts_on) if starts_on == ends_on else f"{one(starts_on)} to {one(ends_on)}"


# ------------------------------- reads ------------------------------- #


@db_operation("steering.live")
def _live(team_id: int, today: date) -> list[Any]:
    from db.models.team_holds import live_holds
    return live_holds(team_id, today)


async def live_rows(team_id: int, today: Optional[date] = None) -> list[Any]:
    from services import schedule_service
    return await _live(team_id, today or schedule_service.get_nba_today())


def enabled() -> bool:
    """`DIME_STEERING_ENABLED`: off, no hold steers anything (the planners read none), no chip is
    applied and no pin is made, so everything runs as it did before Dime could steer."""
    from core.settings import settings
    return settings.dime_steering_enabled


async def holds_for(team_id: int, today: Optional[date] = None) -> list[Hold]:
    """The team's holds in force today or later, while steering is on. Never raises: a failed read
    is no holds, logged, so auto-lineup and the lanes carry on as they would without Dime."""
    if not enabled():
        return []
    try:
        return [hold_of(r) for r in await live_rows(team_id, today)]
    except Exception as exc:
        log.warning("steering_holds_unavailable", team_id=team_id, error=str(exc))
        return []


async def team_fill(team_id: int, state: Any) -> Plan:
    """The fill for a team's board with its holds, as every consumer of the fill reads it (auto-lineup's
    write and email, "Optimize today", the daily actions, Dime's own get_lineup)."""
    from services.lineup_editor_service import planner_players, slot_counts_of
    day = date.fromisoformat(state.nba_date) if getattr(state, "nba_date", None) else None
    holds = await holds_for(team_id, day) if day is not None else []
    return fill_with(planner_players(state), slot_counts_of(state), holds, day)


# ------------------------------- writes ------------------------------- #


@db_operation("steering.clear")
def _clear(team_id: int, hold_id: int, by: str, now: datetime) -> Optional[Any]:
    """Clear one live hold; the row as it then is, or None when it isn't the team's."""
    from db.models.team_holds import TeamHold
    row = TeamHold.get_or_none((TeamHold.id == hold_id) & (TeamHold.team_id == team_id))
    if row is None:
        return None
    if row.cleared_at is None:
        TeamHold.update(cleared_at=now, cleared_by=by).where(
            (TeamHold.id == hold_id) & TeamHold.cleared_at.is_null(True)).execute()
        row = TeamHold.get_by_id(hold_id)
    return row


async def clear(team_id: int, hold_id: int, *, by: str = "user", now: Optional[datetime] = None) -> Optional[Any]:
    return await _clear(team_id, hold_id, by, now or datetime.now(timezone.utc))


@db_operation("steering.pin_benched")
def _pin_benched(row: Any, now: datetime) -> int:
    from db.models.team_holds import TeamHold
    made = 0
    for m in row.moves or []:
        if m.get("role") != "bench":
            continue
        TeamHold.create(team_id=row.team_id_id if hasattr(row, "team_id_id") else row.team_id,
                        user_id=row.user_id_id if hasattr(row, "user_id_id") else row.user_id,
                        kind=PIN, player_id=m["espn_id"], nba_player_id=m.get("nba_player_id"),
                        player_name=m.get("name") or str(m["espn_id"]), player_team=m.get("team"),
                        starts_on=row.nba_date, ends_on=row.nba_date, reason=(row.reason or "")[:REASON_MAX] or "Benched",
                        proposal_id=row.id, applied_by="user", created_at=now)
        made += 1
    return made


async def pin_benched(proposal_id: int, now: Optional[datetime] = None) -> None:
    """A sent lineup chip's benched players, pinned for its day: the fill won't start them again.
    Only while steering is on; never raises (the lineup write is done)."""
    from db.models.ai_conversations import AiProposal
    if not enabled():
        return
    try:
        row = await run_db("steering.pin_row", lambda pid: AiProposal.get_or_none(AiProposal.id == pid), proposal_id)
        if row is None or row.kind != "lineup":
            return
        made = await _pin_benched(row, now or datetime.now(timezone.utc))
        if made:
            log.info("steering_pinned_benched", proposal_id=proposal_id, pins=made)
    except Exception:
        log.exception("steering_pin_failed", proposal_id=proposal_id)
