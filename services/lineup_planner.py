"""
Fill-only lineup planner. Pure: no I/O, no settings, no ORM.

Given today's roster (slot, eligibility, lock, game, injury, value) and the
league's slot counts, produce the fewest moves that put bench players with a
game into slots that are empty or held by players who cannot score today.

Tiers decide who may displace whom:

    A  has a game and is not OUT      never displaced
    B  has a game but is OUT          kept (the status may flip before tip), displaced
                                      only by a tier-A bench player
    C  no game today                  displaced by any A or B bench player

A bench player may take an active slot only when the slot is empty or its holder
is in a strictly worse tier — never a lateral swap, so this is not an optimizer.
Locked players (ESPN's lineupLocked or a started game) never move, and IR / the
unused slots 14-15 are never touched. Moves may chain through unlocked active
players who shift to another eligible active slot (a PG-only bench player can
take the PG slot if the current PG shifts to G and the G holder is the one who
sits); the shortest chain wins.

Every consumer of a plan (manual "Optimize today", the alert email, the auto
write) reads the same `Plan`, so "an issue exists" means exactly "a move exists".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Optional, Sequence

ACTIVE_SLOT_IDS: frozenset[int] = frozenset(range(0, 12))
BENCH_SLOT_ID = 12
IR_SLOT_ID = 13
UNTOUCHABLE_SLOT_IDS: frozenset[int] = frozenset({14, 15})
# Mirrors services.team_insights_service._OUT_STATUSES; DTD / GTD / QUESTIONABLE
# / DOUBTFUL count as healthy — ESPN still scores them if they play.
OUT_STATUSES: frozenset[str] = frozenset({"OUT", "O", "IL", "IL+", "SUSPENSION", "INJURY_RESERVE"})

TIER_A, TIER_B, TIER_C = 0, 1, 2
MAX_CHAIN_HOPS = 4  # filler + up to three shifts + the evicted player


@dataclass(frozen=True)
class PlannerPlayer:
    player_id: int
    name: str
    slot_id: int
    eligible_slot_ids: frozenset[int]
    has_game: bool
    injury_status: Optional[str]
    locked: bool
    value: float = 0.0
    game_note: Optional[str] = None  # "vs LAL · 7:30 PM", supplied by the caller for move notes
    injured: bool = False            # ESPN's `injured` flag — its IR rule ("player is not injured")

    @property
    def is_out(self) -> bool:
        return (self.injury_status or "ACTIVE").upper() in OUT_STATUSES

    @property
    def ir_eligible(self) -> bool:
        """ESPN lists slot 13 in every player's eligibleSlots and enforces "must be
        injured" only when the transaction lands (TRAN_ROSTER_INELIGIBLE_IR_NOT_INJURED),
        so IR eligibility is the injury flag, not the slot list."""
        return IR_SLOT_ID in self.eligible_slot_ids and (self.injured or self.is_out)

    @property
    def tier(self) -> int:
        if not self.has_game:
            return TIER_C
        return TIER_B if self.is_out else TIER_A

    @property
    def playable(self) -> bool:
        """Has a game today and is not OUT (tier A)."""
        return self.tier == TIER_A

    @property
    def bench_note(self) -> str:
        if not self.has_game:
            return "no game today"
        return (self.injury_status or "OUT").upper()


@dataclass(frozen=True)
class Move:
    player_id: int
    from_slot_id: int
    to_slot_id: int
    role: str = "shift"  # start | bench | shift
    note: Optional[str] = None


@dataclass(frozen=True)
class Unfilled:
    player_id: int
    name: str
    slot_id: int
    reason: str  # no_eligible_slot | slot_holder_locked


@dataclass(frozen=True)
class Plan:
    moves: tuple[Move, ...]
    unfilled: tuple[Unfilled, ...]
    summary: str

    @property
    def is_noop(self) -> bool:
        return not self.moves


@dataclass(frozen=True)
class MoveError:
    player_id: Optional[int]
    code: str
    message: str


# ------------------------------- helpers ------------------------------- #


def _active_capacity(slot_counts: Mapping[int, int]) -> dict[int, int]:
    return {s: int(slot_counts.get(s, 0) or 0) for s in ACTIVE_SLOT_IDS}


def _occupants(players: Iterable[PlannerPlayer]) -> dict[int, list[PlannerPlayer]]:
    by_slot: dict[int, list[PlannerPlayer]] = {}
    for p in players:
        by_slot.setdefault(p.slot_id, []).append(p)
    return by_slot


def _by_id(players: Iterable[PlannerPlayer]) -> dict[int, PlannerPlayer]:
    return {p.player_id: p for p in players}


@dataclass(frozen=True)
class _Path:
    """filler -> slots[0]; shifters[i] slots[i] -> slots[i+1]; evicted (if any) slots[-1] -> BE."""
    slots: tuple[int, ...]
    shifters: tuple[PlannerPlayer, ...]
    evicted: Optional[PlannerPlayer]


def _find_path(filler: PlannerPlayer, board: Sequence[PlannerPlayer], capacity: Mapping[int, int]) -> Optional[_Path]:
    """Shortest alternating path from the bench into an eligible slot.

    BFS over (slot, mover). A slot with spare capacity ends the path; a full slot
    ends it when one holder is unlocked and in a strictly worse tier than the
    filler (that holder sits); otherwise any unlocked holder may shift to another
    eligible active slot and the search continues. Among the shortest paths an
    empty slot beats an eviction, then the worst holder (tier C before B, lowest
    value) is the one who sits.
    """
    occupants = _occupants(board)
    start_slots = sorted(s for s in filler.eligible_slot_ids if s in ACTIVE_SLOT_IDS and capacity.get(s, 0) > 0)
    if not start_slots:
        return None

    queue: deque[tuple[tuple[int, ...], tuple[PlannerPlayer, ...]]] = deque()
    visited_players = {filler.player_id}
    for s in start_slots:
        queue.append(((s,), ()))

    best: Optional[tuple[tuple[int, int, float, int], _Path]] = None
    best_depth: Optional[int] = None

    while queue:
        slots, shifters = queue.popleft()
        depth = len(slots)
        if best_depth is not None and depth > best_depth:
            break
        slot = slots[-1]
        holders = occupants.get(slot, [])
        if len(holders) < capacity.get(slot, 0):
            candidate = ((0, 0, 0.0, slot), _Path(slots, shifters, None))
            if best is None or candidate[0] < best[0]:
                best, best_depth = candidate, depth
            continue
        # BFS is depth-monotone: any candidate found here is at `best_depth` or is the first.
        shifting = {s.player_id for s in shifters}
        for holder in holders:
            if holder.locked or holder.player_id in shifting:
                continue
            if holder.tier > filler.tier:
                candidate = ((1, -holder.tier, holder.value, slot), _Path(slots, shifters, holder))
                if best is None or candidate[0] < best[0]:
                    best, best_depth = candidate, depth
        if best_depth is not None and depth >= best_depth:
            continue
        if depth >= MAX_CHAIN_HOPS:
            continue
        for holder in holders:
            if holder.locked or holder.player_id in visited_players:
                continue
            for nxt in sorted(holder.eligible_slot_ids):
                if nxt == slot or nxt not in ACTIVE_SLOT_IDS or capacity.get(nxt, 0) <= 0 or nxt in slots:
                    continue
                visited_players.add(holder.player_id)
                queue.append((slots + (nxt,), shifters + (holder,)))
    return best[1] if best else None


def _apply_path(filler: PlannerPlayer, path: _Path) -> list[Move]:
    moves = [Move(filler.player_id, BENCH_SLOT_ID, path.slots[0], role="start", note=filler.game_note)]
    for i, shifter in enumerate(path.shifters):
        moves.append(Move(shifter.player_id, path.slots[i], path.slots[i + 1], role="shift"))
    if path.evicted is not None:
        moves.append(Move(path.evicted.player_id, path.slots[-1], BENCH_SLOT_ID, role="bench", note=path.evicted.bench_note))
    return moves


def apply_moves(players: Sequence[PlannerPlayer], moves: Iterable[Move]) -> list[PlannerPlayer]:
    """The board after `moves`, applied simultaneously (swaps are pairs)."""
    target: dict[int, int] = {m.player_id: m.to_slot_id for m in moves}
    return [replace(p, slot_id=target[p.player_id]) if p.player_id in target else p for p in players]


def _unfilled_reason(filler: PlannerPlayer, board: Sequence[PlannerPlayer],
                     capacity: Mapping[int, int]) -> Optional[str]:
    """Why a bench player with a game stayed there — None when there is nothing to
    say (every active slot he could reach is held by someone at least as good)."""
    occupants = _occupants(board)
    locked_worse = False
    for s in filler.eligible_slot_ids:
        if s not in ACTIVE_SLOT_IDS or capacity.get(s, 0) <= 0:
            continue
        if any(h.locked and h.tier > filler.tier for h in occupants.get(s, [])):
            locked_worse = True
    if locked_worse:
        return "slot_holder_locked"
    if any(p.slot_id in ACTIVE_SLOT_IDS and p.tier > filler.tier for p in board):
        return "no_eligible_slot"  # a worse player starts in a slot this player cannot reach
    return None


# ------------------------------- public API ------------------------------- #


def plan_fill(players: Sequence[PlannerPlayer], slot_counts: Mapping[int, int]) -> Plan:
    capacity = _active_capacity(slot_counts)
    board = list(players)
    moves: list[Move] = []
    unfilled: list[Unfilled] = []

    fillers = sorted(
        (p for p in board if p.slot_id == BENCH_SLOT_ID and not p.locked and p.tier in (TIER_A, TIER_B)),
        key=lambda p: (p.tier, -p.value, p.name),
    )
    for filler in fillers:
        current = _by_id(board)[filler.player_id]
        if current.slot_id != BENCH_SLOT_ID:
            continue
        path = _find_path(current, board, capacity)
        if path is None:
            reason = _unfilled_reason(current, board, capacity) if current.tier == TIER_A else None
            if reason:
                unfilled.append(Unfilled(current.player_id, current.name, current.slot_id, reason))
            continue
        path_moves = _apply_path(current, path)
        moves.extend(path_moves)
        board = apply_moves(board, path_moves)

    return Plan(tuple(moves), tuple(unfilled), _summary(moves, unfilled, _by_id(players)))


def _summary(moves: Sequence[Move], unfilled: Sequence[Unfilled], names: Mapping[int, PlannerPlayer]) -> str:
    if not moves:
        return "Nothing to fill: every bench player with a game is blocked or the lineup is already set."
    starts = [m for m in moves if m.role == "start"]
    benches = [m for m in moves if m.role == "bench"]
    parts = [f"start {names[m.player_id].name}" for m in starts]
    parts += [f"bench {names[m.player_id].name} ({m.note})" for m in benches]
    text = f"{len(moves)} move(s): " + ", ".join(parts)
    if unfilled:
        text += f"; {len(unfilled)} still on the bench"
    return text


def _slot_label(slot_id: int) -> str:
    from utils.espn_helpers import POSITION_MAP  # a plain dict; the planner stays I/O-free
    label = POSITION_MAP.get(slot_id)
    return label if isinstance(label, str) and label else str(slot_id)


def _ineligible_message(player: PlannerPlayer, to_slot_id: int) -> str:
    """Why a move is refused before it reaches ESPN. Eligibility is ESPN's own list
    (`eligibleSlots`); IR appears on it only for players ESPN has marked OUT."""
    if to_slot_id == IR_SLOT_ID:
        return f"{player.name} can't go on IR — ESPN only allows players it lists as injured (OUT) there"
    eligible = [_slot_label(s) for s in sorted(player.eligible_slot_ids) if s in ACTIVE_SLOT_IDS]
    where = f" (eligible: {', '.join(eligible)})" if eligible else ""
    return f"{player.name} isn't eligible at {_slot_label(to_slot_id)}{where}"


def validate_moves(players: Sequence[PlannerPlayer], slot_counts: Mapping[int, int],
                   moves: Sequence[Move]) -> list[MoveError]:
    """Why a user-submitted set of moves cannot be sent as one ESPN transaction (empty = fine)."""
    errors: list[MoveError] = []
    by_id = _by_id(players)
    seen: set[int] = set()
    for m in moves:
        if m.player_id in seen:
            errors.append(MoveError(m.player_id, "DUPLICATE_PLAYER", "A player may appear in at most one move"))
            continue
        seen.add(m.player_id)
        player = by_id.get(m.player_id)
        if player is None:
            errors.append(MoveError(m.player_id, "UNKNOWN_PLAYER", "Player is not on this roster"))
            continue
        if m.from_slot_id == m.to_slot_id:
            errors.append(MoveError(m.player_id, "SAME_SLOT", f"{player.name} is already in that slot"))
            continue
        if m.from_slot_id in UNTOUCHABLE_SLOT_IDS or m.to_slot_id in UNTOUCHABLE_SLOT_IDS:
            errors.append(MoveError(m.player_id, "UNTOUCHABLE_SLOT", "That slot cannot be edited"))
            continue
        if m.from_slot_id != player.slot_id:
            errors.append(MoveError(m.player_id, "STALE_SLOT",
                                    f"{player.name} is no longer in that slot — refresh the lineup"))
            continue
        if player.locked:
            errors.append(MoveError(m.player_id, "LOCKED", f"{player.name} is locked (game started)"))
            continue
        eligible = (player.ir_eligible if m.to_slot_id == IR_SLOT_ID
                    else m.to_slot_id == BENCH_SLOT_ID or m.to_slot_id in player.eligible_slot_ids)
        if not eligible:
            errors.append(MoveError(m.player_id, "INELIGIBLE", _ineligible_message(player, m.to_slot_id)))
            continue
    if errors:
        return errors

    after = apply_moves(players, moves)
    occupancy = _occupants(after)
    for slot_id, holders in sorted(occupancy.items()):
        if slot_id in UNTOUCHABLE_SLOT_IDS:
            continue
        limit = int(slot_counts.get(slot_id, 0) or 0)
        if len(holders) > limit:
            errors.append(MoveError(None, "CAPACITY",
                                    f"Slot {slot_id} would hold {len(holders)} players (limit {limit})"))
    return errors
