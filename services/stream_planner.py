"""
The stream lane's optimizer: who should hold one roster spot on each remaining day
of the matchup, to make the most of the adds left.

Pure. The caller supplies, for each day of the horizon, the rest of the roster
(each player's projected value if he plays that day, and his active slots) and
the same for every candidate; `spot_values` turns that into what a player adds to
each day's best lineup when he holds the spot, and `plan_stream` finds the plan.

The plan maximizes projected points over the horizon given the adds left. There
is no rule about when to switch: whether to replace a player who plays, or wait
for his day off, falls out of the maximization. The constraints are the budget;
the spot changing hands at most once a day, and only on a day a switch may take
effect (`switchable`: e.g. that day's allowance already spent); a candidate's
first available day (waivers); and the waiver gap: a player we drop, the holder
included, sits on waivers before he can be added back (`rehold_after` days).

Method. An exact dynamic program over (day, holder, adds left) ignores the gap,
so its value bounds every plan from each state. Its best plan usually respects
the gap and is then the answer. When it takes someone back too soon, a
branch-and-bound search over the same choices, pruned by those bounds and by the
states it has already reached, finds the best plan that doesn't. Only players
dropped within the gap need tracking, which keeps the search small: realistic
weeks and two-week playoff rounds solve exactly in milliseconds. (Forbidding
re-adds outright, `rehold_after=None`, is much harder over two weeks; the
search then stops at `MAX_NODES` and says the plan isn't proven best.)

Ties go to keeping the holder: a later switch commits less, and the lane
re-plans every day anyway. Values are floats; anything within `EPS` is a tie.
Every switch also costs `NUDGE`, far below any real value, so of two plans worth
the same, the one with fewer adds wins outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from services.lineup_planner import ACTIVE_SLOT_IDS

EPS = 1e-9
NUDGE = 1e-6
# A safety valve for the search; a plan found under it is still feasible, just not proven best.
MAX_NODES = 200_000


# ------------------------------- one day's lineup ------------------------------- #


@dataclass(frozen=True)
class DayPlayer:
    """A player in one day's lineup: his projected value if he plays that day (0 with no game, or
    out), and the active slots he may fill."""

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
    """What one player adds to each day's best lineup when he holds the spot: the lineup with him
    and the rest of the roster, less the rest alone. 0 on a day he doesn't play or wouldn't start.
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
    """A player who could hold the spot: his value on each day of the horizon (`spot_values`), and
    the first day he can (waivers clearing; the holder's is 0)."""

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
    exact: bool = True                # False only if the search hit MAX_NODES

    @property
    def gain(self) -> float:
        return self.value - self.hold_value

    @property
    def first_move(self) -> Optional[Stretch]:
        """The switch to make now: a new player taking the spot on day 0. None means keep the holder
        today, whatever the plan does later (the lane re-plans before each day)."""
        first = self.stretches[0] if self.stretches else None
        return first if first is not None and first.player_id != self.holder_id else None


def plan_stream(
    holder: Candidate,
    candidates: Sequence[Candidate],
    *,
    adds: Optional[int],
    switchable: Optional[Sequence[bool]] = None,
    rehold_after: Optional[int] = 2,
) -> StreamPlan:
    """The best plan for the spot over the horizon (`len(holder.values)` days). `adds` is the budget
    left (None = no limit); `switchable[d]` says whether a switch may take effect on day d. A player
    dropped by a switch that takes effect on day s can hold the spot again from day s + `rehold_after`
    (he sits on waivers in between; None = never), the holder included."""
    horizon = len(holder.values)
    if any(len(c.values) != horizon for c in candidates):
        raise ValueError("every candidate needs a value for each day of the horizon")
    can_switch = list(switchable) if switchable is not None else [True] * horizon
    if len(can_switch) != horizon:
        raise ValueError("switchable needs one entry per day of the horizon")
    gap = horizon + 1 if rehold_after is None else max(1, int(rehold_after))

    players = [holder, *(c for c in candidates if c.player_id != holder.player_id)]
    values = [p.values for p in players]
    budget = horizon if adds is None else max(0, min(int(adds), horizon))
    n = len(players)

    def can_take(j: int, d: int) -> bool:
        """Player j may be switched in to hold the spot from day d (waivers aside)."""
        return can_switch[d] and d >= players[j].available_from

    # best[d][i][b]: the most the spot can score on days d.. when player i holds it going into day d
    # with b adds left, ignoring the waiver gap.
    best = [[[0.0] * (budget + 1) for _ in range(n)] for _ in range(horizon + 1)]
    for d in range(horizon - 1, -1, -1):
        nxt, here = best[d + 1], best[d]
        for b in range(budget + 1):
            # The two best switches into day d (a player can't switch to himself).
            top: list[tuple[float, int]] = []
            if b > 0:
                for j in range(n):
                    if can_take(j, d):
                        top.append((values[j][d] + nxt[j][b - 1] - NUDGE, j))
                top.sort(key=lambda t: (-t[0], players[t[1]].player_id))
                top = top[:2]
            for i in range(n):
                keep = values[i][d] + nxt[i][b]
                switch = next((v for v, j in top if j != i), None)
                here[i][b] = keep if switch is None or keep >= switch - EPS else switch

    def choices(d: int, i: int, b: int, waiting: frozenset[int]) -> list[tuple[float, int]]:
        """The moves into day d, best bound first and keeping first on a tie: (bound, next holder).
        `waiting` are the players still on waivers from a drop."""
        out = [(values[i][d] + best[d + 1][i][b], i)]
        if b > 0:
            out += [(values[j][d] + best[d + 1][j][b - 1] - NUDGE, j) for j in range(n)
                    if j != i and j not in waiting and can_take(j, d)]
        return sorted(out, key=lambda t: (-t[0], t[1] != i, players[t[1]].player_id))

    # The program's own best plan (keep on ties), and whether it takes anyone back too soon.
    path, i, b = [], 0, budget
    for d in range(horizon):
        _, j = choices(d, i, b, frozenset())[0]
        if j != i:
            b -= 1
        path.append(j)
        i = j
    if _too_soon(path, gap):
        path, exact = _search(horizon, budget, gap, values, best, choices)
    else:
        exact = True
    return _plan(players, values, path, exact)


def _too_soon(path: Sequence[int], gap: int) -> bool:
    """Whether the plan takes a dropped player back before his waivers clear."""
    clears: dict[int, int] = {}
    holder = 0
    for d, j in enumerate(path):
        if j == holder:
            continue
        if clears.get(j, -1) > d:
            return True
        clears[holder] = d + gap
        holder = j
    return False


def _search(horizon, budget, gap, values, best, choices) -> tuple[list[int], bool]:
    """Branch and bound over the program's choices, honouring the waiver gap. The holder keeping the
    spot every day is the first incumbent; the program's values prune, and so does reaching a
    state (day, holder, adds left, who is still on waivers until when) no better than before."""
    found_value = sum(values[0][d] for d in range(horizon))
    found = [0] * horizon
    nodes = 0
    exact = True
    reached: dict[tuple, float] = {}

    def go(d: int, i: int, b: int, waivers: tuple[tuple[int, int], ...], acc: float, path: list[int]) -> None:
        nonlocal found_value, found, nodes, exact
        if not exact:
            return
        nodes += 1
        if nodes > MAX_NODES:
            exact = False
            return
        if d == horizon:
            if acc > found_value + EPS:
                found_value, found = acc, list(path)
            return
        if acc + best[d][i][b] <= found_value + EPS:
            return
        live = tuple(w for w in waivers if w[1] > d)
        key = (d, i, b, live)
        if reached.get(key, -1.0) >= acc - EPS:
            return
        reached[key] = acc
        for _, j in choices(d, i, b, frozenset(p for p, _ in live)):
            path.append(j)
            if j == i:
                go(d + 1, i, b, live, acc + values[i][d], path)
            else:
                go(d + 1, j, b - 1, tuple(sorted((*live, (i, d + gap)))), acc + values[j][d] - NUDGE, path)
            path.pop()

    go(0, 0, budget, (), 0.0, [])
    return found, exact


def _plan(players: Sequence[Candidate], values, path: Sequence[int], exact: bool) -> StreamPlan:
    stretches: list[Stretch] = []
    for d, j in enumerate(path):
        if stretches and stretches[-1].player_id == players[j].player_id:
            last = stretches[-1]
            stretches[-1] = Stretch(last.player_id, last.start, d + 1, last.value + values[j][d])
        else:
            stretches.append(Stretch(players[j].player_id, d, d + 1, values[j][d]))
    holder_id = players[0].player_id
    return StreamPlan(
        holder_id=holder_id,
        holders=tuple(players[j].player_id for j in path),
        stretches=tuple(stretches),
        value=sum(s.value for s in stretches),
        hold_value=sum(values[0]),
        adds=len(stretches) - (1 if stretches and stretches[0].player_id == holder_id else 0),
        exact=exact,
    )
