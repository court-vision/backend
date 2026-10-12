"""
The stream lane's optimizer: who should hold one roster spot on each remaining day
of the matchup, to make the most of the adds left.

Pure. The caller supplies, for each day of the horizon, the rest of the roster
(each player's projected value if they play that day, and their active slots) and
the same for every candidate; `spot_values` turns that into what a player adds to
each day's best lineup when holding the spot, and `plan_stream` finds the plan.

The plan maximizes projected points over the horizon given the adds left. There
is no rule about when to switch: whether to replace a player who plays, or wait
for their day off, falls out of the maximization. The constraints are the budget;
the spot changing hands at most once a day, and only on a day a switch may take
effect (`switchable`: e.g. that day's allowance already spent); a candidate's
first available day (waivers); and the waiver gap: a player we drop, the holder
included, sits on waivers before they can be added back (`rehold_after` days).

Method. An exact dynamic program over (day, holder, adds left, the player dropped
the day before). The spot changes hands at most once a day, so with a gap of at
most two days the only player still on waivers going into a day is the one
dropped the day before, and that's all the state needs. That player matters
only when they would have been the best switch, so each state keeps its two
best switches, and the program costs about what one without waivers would.
Longer gaps would need more of the history in the state; every league seen so
far has 24-hour waivers, so they're refused rather than approximated.

Plans are compared as whole numbers: points to the nearest `EPS`, then fewer
adds. Points always come first; of two plans worth the same, the one with fewer
adds wins. Of plans equal on both, the holder keeps the spot as long as that
costs nothing: a later switch commits less, and the lane re-plans every day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from services.lineup_planner import ACTIVE_SLOT_IDS

# Points closer than this are equal.
EPS = 1e-9
# The longest waiver gap the program's state covers (see the module docstring).
MAX_REHOLD_AFTER = 2


# ------------------------------- one day's lineup ------------------------------- #


@dataclass(frozen=True)
class DayPlayer:
    """A player in one day's lineup: their projected value if they play that day (0 with no game,
    or out), and the active slots they may fill."""

    player_id: int
    value: float
    eligible: frozenset[int]


def best_lineup_value(players: Sequence[DayPlayer], slot_counts: Mapping[int, int]) -> float:
    """The most a day's starters can score. Players are seated highest value first, each through
    an augmenting path that keeps everyone already seated: with the value on players alone, this
    greedy is exact (the seated sets form a matroid)."""
    seats = [slot for slot, count in sorted(slot_counts.items()) if slot in ACTIVE_SLOT_IDS
             for _ in range(max(0, int(count or 0)))]
    owner: list[Optional[int]] = [None] * len(seats)

    def seat(i: int, seen: set[int]) -> bool:
        for j, slot in enumerate(seats):
            if j in seen or slot not in players[i].eligible:
                continue
            seen.add(j)
            holder = owner[j]
            if holder is None or seat(holder, seen):
                owner[j] = i
                return True
        return False

    total = 0.0
    for i in sorted(range(len(players)), key=lambda i: (-players[i].value, players[i].player_id)):
        if players[i].value > EPS and seat(i, set()):
            total += players[i].value
    return total


def spot_values(
    rest_by_day: Sequence[Sequence[DayPlayer]],
    slot_counts: Mapping[int, int],
    player_by_day: Sequence[Optional[DayPlayer]],
    *,
    bases: Optional[Sequence[float]] = None,
) -> tuple[float, ...]:
    """What one player adds to each day's best lineup when holding the spot: the lineup with them
    and the rest of the roster, less the rest alone. 0 on a day they don't play or wouldn't start.
    `bases` (the rest's own lineup value per day) can be passed to share the work across players."""
    out = []
    for d, rest in enumerate(rest_by_day):
        me = player_by_day[d]
        if me is None or me.value <= EPS:
            out.append(0.0)
            continue
        base = bases[d] if bases is not None else best_lineup_value(rest, slot_counts)
        out.append(max(0.0, best_lineup_value([*rest, me], slot_counts) - base))
    return tuple(out)


# ------------------------------- the plan ------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """A player who could hold the spot: their value on each day of the horizon (`spot_values`),
    and the first day they can (waivers clearing; the holder's is 0)."""

    player_id: int
    values: tuple[float, ...]
    available_from: int = 0


@dataclass(frozen=True)
class Stretch:
    """One player holding the spot from day `start` up to (not including) `end`."""

    player_id: int
    start: int
    end: int
    value: float


@dataclass(frozen=True)
class StreamPlan:
    holder_id: int
    holders: tuple[int, ...]          # who holds the spot each day of the horizon
    stretches: tuple[Stretch, ...]    # the same, as runs, in day order
    value: float                      # projected points from the spot over the horizon
    hold_value: float                 # the holder keeping it every day
    adds: int

    @property
    def gain(self) -> float:
        return self.value - self.hold_value

    @property
    def first_move(self) -> Optional[Stretch]:
        """The switch to make now: a new player taking the spot on day 0. None means keep the holder
        today, whatever the plan does later (the lane re-plans before each day)."""
        first = self.stretches[0] if self.stretches else None
        return first if first is not None and first.player_id != self.holder_id else None


# A state's moves into its day: keeping the holder's score, then the two best switches (score,
# player index each).
_Moves = tuple[int, Optional[int], Optional[int], Optional[int], Optional[int]]


def _switch(moves: _Moves, dropped: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    """The best switch a state allows: the second best when the best is the player dropped the day
    before."""
    _, first, first_to, second, second_to = moves
    return (second, second_to) if dropped is not None and first_to == dropped else (first, first_to)


def plan_stream(
    holder: Candidate,
    candidates: Sequence[Candidate],
    *,
    adds: Optional[int],
    switchable: Optional[Sequence[bool]] = None,
    rehold_after: int = 2,
) -> StreamPlan:
    """The best plan for the spot over the horizon (`len(holder.values)` days). `adds` is the budget
    left (None = no limit); `switchable[d]` says whether a switch may take effect on day d. A player
    dropped by a switch that takes effect on day s can hold the spot again from day s + `rehold_after`
    (1 or 2), the holder included."""
    horizon = len(holder.values)
    if any(len(c.values) != horizon for c in candidates):
        raise ValueError("every candidate needs a value for each day of the horizon")
    can_switch = list(switchable) if switchable is not None else [True] * horizon
    if len(can_switch) != horizon:
        raise ValueError("switchable needs one entry per day of the horizon")
    if rehold_after not in range(1, MAX_REHOLD_AFTER + 1):
        raise ValueError(f"rehold_after must be 1 to {MAX_REHOLD_AFTER} days, not {rehold_after!r}")
    waivers = rehold_after == 2      # the player dropped the day before can't come straight back

    players = [holder, *(c for c in candidates if c.player_id != holder.player_id)]
    ids = [p.player_id for p in players]
    n = len(players)
    budget = horizon if adds is None else max(0, min(int(adds), horizon))
    # Scores are whole numbers: points in units of EPS, times (horizon + 1), less one per add. A
    # plan has at most `horizon` adds, so points always decide and adds only break a tie in points.
    weight = horizon + 1
    score = [[round(v / EPS) * weight for v in p.values] for p in players]

    def available(j: int, d: int) -> bool:
        """Player j may be switched in to hold the spot from day d (yesterday's drop aside)."""
        return can_switch[d] and d >= players[j].available_from

    # moves[d][b][i]: player i holds the spot going into day d with b adds left. Keeping scores
    # today's points plus the best from tomorrow; a switch to j scores j's points today, less the
    # add, plus the best from tomorrow with j holding and, with waivers, i unable to come back.
    moves: list[list[list[_Moves]]] = [[[] for _ in range(budget + 1)] for _ in range(horizon)]

    def best(d: int, b: int, i: int, dropped: Optional[int] = None) -> int:
        """The best score from day d on, with `dropped` (the player dropped the day before) barred."""
        if d == horizon:
            return 0
        switch, _ = _switch(moves[d][b][i], dropped)
        keep = moves[d][b][i][0]
        return keep if switch is None else max(keep, switch)

    for d in range(horizon - 1, -1, -1):
        for b in range(budget + 1):
            keeps = [score[i][d] + best(d + 1, b, i) for i in range(n)]
            takers = [j for j in range(n) if available(j, d)] if b > 0 else []
            free = {j: score[j][d] - 1 + best(d + 1, b - 1, j) for j in takers}
            # Taking j from i costs more only when i would have been j's best switch tomorrow:
            # `barred_for[i]` lists those j with what the switch scores instead.
            barred_for: dict[int, list[tuple[int, int]]] = {}
            bars: dict[int, int] = {}
            if waivers and d + 1 < horizon:
                for j in takers:
                    keep, first, first_to, second, _ = moves[d + 1][b - 1][j]
                    if first_to is not None and first > keep:
                        bars[j] = first_to
                        after = keep if second is None else max(keep, second)
                        barred_for.setdefault(first_to, []).append((score[j][d] - 1 + after, j))
            order = sorted(takers, key=lambda j: (-free[j], ids[j]))
            # From i, a switch to anyone ranked below i never beats keeping i (keeping scores more
            # than switching to i would), so when i is in the top two, the other one is enough.
            top = order[:2]
            row: list[_Moves] = []
            for i in range(n):
                barred = barred_for.get(i)
                if not barred:
                    options = [(free[j], j) for j in top if j != i]
                else:
                    options = list(barred)
                    found = 0
                    for j in order:
                        if j == i or bars.get(j) == i:
                            continue
                        options.append((free[j], j))
                        found += 1
                        if found == 2:
                            break
                    options.sort(key=lambda o: (-o[0], ids[o[1]]))
                (first, first_to), (second, second_to) = (options + [(None, None)] * 2)[:2]
                row.append((keeps[i], first, first_to, second, second_to))
            moves[d][b] = row

    # Walk the best plan forward: switch only when it scores strictly more than keeping.
    path: list[int] = []
    i, b, dropped = 0, budget, None
    for d in range(horizon):
        switch, to = _switch(moves[d][b][i], dropped)
        if to is not None and switch > moves[d][b][i][0]:
            i, b, dropped = to, b - 1, (i if waivers else None)
        else:
            dropped = None
        path.append(i)
    return _plan(players, path)


def _plan(players: Sequence[Candidate], path: Sequence[int]) -> StreamPlan:
    stretches: list[Stretch] = []
    for d, j in enumerate(path):
        value = players[j].values[d]
        if stretches and stretches[-1].player_id == players[j].player_id:
            last = stretches[-1]
            stretches[-1] = Stretch(last.player_id, last.start, d + 1, last.value + value)
        else:
            stretches.append(Stretch(players[j].player_id, d, d + 1, value))
    holder_id = players[0].player_id
    return StreamPlan(
        holder_id=holder_id,
        holders=tuple(players[j].player_id for j in path),
        stretches=tuple(stretches),
        value=sum(s.value for s in stretches),
        hold_value=sum(players[0].values),
        adds=len(stretches) - (1 if stretches and stretches[0].player_id == holder_id else 0),
    )
