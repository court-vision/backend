"""
Several streamed roster spots planned together: who should hold each of K spots on
each remaining day of the matchup, to make the most of the adds left. One spot is
`services.stream_planner`'s (an exact program too, and about ten times faster); this
is the same problem for any number of spots.

Pure. The caller supplies, for each day of the horizon, the rest of the roster (the
players not in a spot, as `DayPlayer`s) and, for every player who could hold a spot
(the spots' players now, and the free agents), what they would score each day and
the active slots they may fill.

What changes with more than one spot:

- The spots share the adds: the budget, and a per-day limit (one add a day across
  every spot), so they can't be planned one at a time.
- Days stop adding up. Two streamers can compete for the same seats (two C-only
  players and one open C/UT), so the pair is worth less than the two apart. A day
  is worth what the spots' players add to its best lineup together.
- A free agent holds one spot at a time, and one dropped from any spot waits out
  their waivers before any spot takes them back.
- A dynamic program's state would be every set of holders: players^spots of them.

Method. An integer program, solved by HiGHS:

    x[j,d]    player j holds a spot on day d (0/1)
    a[j,d]    j is added on day d: a[j,d] >= x[j,d] - x[j,d-1]
    y[p,t,d]  player p (the rest of the roster, or a spot's) starts in a slot of type t on day d

    each day          sum_j x[j,d] = spots; a slot type seats at most its count;
                      a spot's player starts only on a day they hold a spot (y <= x)
    adds              sum a <= the budget; sum_j a[j,d] <= day d's limit
    waivers           a player dropped on day s holds no spot again before s + rehold_after:
                      x[j,s-1] - x[j,s] + x[j,t] <= 1 for s < t < s + rehold_after
    a free agent      x[j,d] = 0 before their own waivers clear
    position limits   the spots' players at a limited position fit that day's room

Each day's lineup is an assignment problem, so once x is whole the seats are too: y
needn't be integer. Any waiver gap can be planned, not only one or two days.

Plans are compared as the one-spot planner compares them, in one objective with
whole-number weights: points (to the nearest `UNIT`) first, then fewer adds, then
fewer switches today (a later switch commits less, and the lanes re-plan every
day). HiGHS proves the optimum. On weeks shaped like the real ones (150 free agents,
one to four spots, an add a day) it did so at the root of its search every time,
in about 0.1 s for a week and 0.2-0.4 s for a two-week round, with the solve
outside the GIL.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import highspy
import numpy as np

from services.lineup_planner import ACTIVE_SLOT_IDS
from services.stream_planner import EPS, DayPlayer, Stretch, best_lineup_value

# Points are compared to the nearest this: the objective's weights are whole numbers.
UNIT = 0.01
# A plan HiGHS hasn't proven by then is returned as found (`optimal` False).
TIME_LIMIT_S = 10.0


@dataclass(frozen=True)
class Streamer:
    """A player who could hold a spot: their value on each day of the horizon if they play (0 with
    no game, or out), the active slots they may fill, the first day they can take a spot (waivers
    clearing; a spot's player now: 0), and their default position (for position limits)."""

    player_id: int
    values: tuple[float, ...]
    eligible: frozenset[int]
    available_from: int = 0
    position: Optional[int] = None


@dataclass(frozen=True)
class Switch:
    """Spot `spot` changes hands on day `day`: `drop_id` out, `add_id` in."""

    day: int
    spot: int
    drop_id: int
    add_id: int


@dataclass(frozen=True)
class MultiStreamPlan:
    holder_ids: tuple[int, ...]                  # each spot's player now
    spots: tuple[tuple[int, ...], ...]           # spots[k][d]: who holds spot k on day d
    # Per spot, its players as runs in day order. A run's value is what its player adds to each
    # day's best lineup with the other spots' players in it, so two spots competing for one seat
    # can each be worth less than the day's value together.
    stretches: tuple[tuple[Stretch, ...], ...]
    day_values: tuple[float, ...]                # what the spots' players add to each day's best lineup
    value: float                                 # their sum over the horizon
    hold_value: float                            # the spots' players now kept every day
    adds: int
    switches: tuple[Switch, ...]                 # in day order
    optimal: bool                                # proven best (False: the best found in the time limit)
    marginals: tuple[tuple[float, ...], ...] = ()   # marginals[k][d]: the stretches' values, day by day
    # Per spot: what its player now would add keeping it every day, the other spots as planned. With
    # one spot, `hold_value`.
    hold_values: tuple[float, ...] = ()
    considered: frozenset[int] = frozenset()     # the free agents that could add to a day in a spot

    @property
    def gain(self) -> float:
        return self.value - self.hold_value

    @property
    def first_moves(self) -> tuple[Switch, ...]:
        """The switches to make now: those taking effect on day 0. Empty means keep every spot's
        player today, whatever the plan does later (the lanes re-plan before each day)."""
        return tuple(s for s in self.switches if s.day == 0)


class _Rows:
    """A row-wise model under construction: bounds, and each row's coefficients."""

    def __init__(self) -> None:
        self.col_lower: list[float] = []
        self.col_upper: list[float] = []
        self.row_lower: list[float] = []
        self.row_upper: list[float] = []
        self.starts: list[int] = []
        self.index: list[int] = []
        self.value: list[float] = []

    def col(self, upper: float = 1.0) -> int:
        self.col_lower.append(0.0)
        self.col_upper.append(upper)
        return len(self.col_lower) - 1

    def row(self, lower: float, upper: float, cols: Sequence[int], coefs: Sequence[float]) -> None:
        self.row_lower.append(lower)
        self.row_upper.append(upper)
        self.starts.append(len(self.index))
        self.index.extend(cols)
        self.value.extend(coefs)


def plan_streams(
    holders: Sequence[Streamer],
    candidates: Sequence[Streamer],
    rest_by_day: Sequence[Sequence[DayPlayer]],
    slot_counts: Mapping[int, int],
    *,
    adds: Optional[int],
    day_limits: Optional[Sequence[Optional[int]]] = None,
    rehold_after: int = 2,
    position_room: Optional[Mapping[int, Sequence[int]]] = None,
    time_limit: float = TIME_LIMIT_S,
) -> MultiStreamPlan:
    """The best plan for the spots `holders` hold, over the horizon (`len(rest_by_day)` days).

    `adds` is the budget left across every spot (None = no limit); `day_limits[d]` the most
    switches that may take effect on day d (None = one per spot; 0 closes the day). A player
    dropped by a switch that takes effect on day s can hold a spot again from day s +
    `rehold_after`, the spots' players now included. `position_room[pos][d]` is how many of the
    spots' players may have default position `pos` on day d (ESPN's limit less the rest of the
    roster's); keeping the spots' players now is always allowed, whatever it says."""
    horizon = len(rest_by_day)
    spots = len(holders)
    if spots == 0:
        raise ValueError("there must be at least one spot")
    holder_ids = [h.player_id for h in holders]
    if len(set(holder_ids)) != spots:
        raise ValueError("a player can hold only one spot")
    players = [*holders, *(c for c in candidates if c.player_id not in set(holder_ids))]
    if len({p.player_id for p in players}) != len(players):
        raise ValueError("a player can be a candidate only once")
    if any(len(p.values) != horizon for p in players):
        raise ValueError("every player needs a value for each day of the horizon")
    limits = list(day_limits) if day_limits is not None else [None] * horizon
    if len(limits) != horizon:
        raise ValueError("day_limits needs one entry per day of the horizon")
    if rehold_after < 1:
        raise ValueError(f"rehold_after must be at least 1 day, not {rehold_after!r}")
    room = {pos: list(days) for pos, days in (position_room or {}).items()}
    if any(len(days) != horizon for days in room.values()):
        raise ValueError("position_room needs one entry per day of the horizon")

    seats = {s: int(n) for s, n in slot_counts.items() if s in ACTIVE_SLOT_IDS and int(n or 0) > 0}

    bases = [best_lineup_value(rest, seats) for rest in rest_by_day]
    held_at = Counter(h.position for h in holders)

    def may_hold(p: Streamer, d: int) -> bool:
        """Day d's position room has a place for them (the spots' players there may make it)."""
        return p.position not in room or max(room[p.position][d], held_at[p.position]) > 0

    def adds_to(p: Streamer, d: int) -> bool:
        """They add to day d's best lineup over the rest of the roster: the most they could add in
        a spot, since the other spots' players only take seats from them."""
        if p.values[d] <= EPS or not any(s in seats for s in p.eligible):
            return False
        me = DayPlayer(p.player_id, p.values[d], p.eligible)
        return best_lineup_value([*rest_by_day[d], me], seats) - bases[d] > EPS

    day_caps = [spots if lim is None else max(0, min(spots, int(lim))) for lim in limits]
    most_adds = sum(day_caps) if adds is None else min(sum(day_caps), max(0, int(adds)))
    # A free agent who adds nothing to any day they could hold a spot is worth nothing in one.
    # They can still be the one a position limit needs to fill a spot whose player must leave it;
    # any of them will do (by position, if limited), and no plan adds more than `most_adds`.
    starters, fillers = [], {}
    for p in players[spots:]:
        days = [d for d in range(p.available_from, horizon) if may_hold(p, d)]
        if any(adds_to(p, d) for d in days):
            starters.append(p)
        elif room and days:
            fillers.setdefault(p.position if p.position in room else None, []).append(p)
    considered = frozenset(p.player_id for p in starters)
    players = [*players[:spots], *starters,
               *(p for group in fillers.values()
                 for p in sorted(group, key=lambda p: (p.available_from, p.player_id))[:most_adds])]
    if horizon == 0 or most_adds == 0 or len(players) == spots:
        return _plan(holders, players, [frozenset(holder_ids)] * horizon, rest_by_day, seats, optimal=True,
                     considered=considered)

    inf = highspy.kHighsInf
    m = _Rows()
    n = len(players)
    x = [[m.col(1.0 if j < spots or d >= p.available_from else 0.0) for d in range(horizon)]
         for j, p in enumerate(players)]
    # A spot's player is held going in: they can't be added on day 0.
    a = [[m.col(0.0 if j < spots and d == 0 else 1.0) for d in range(horizon)] for j in range(n)]
    points: dict[int, int] = {}          # y column -> its player's points that day, in UNITs
    seated: dict[tuple[int, int], list[int]] = {(s, d): [] for s in seats for d in range(horizon)}

    def seat(value: float, eligible: frozenset[int], d: int, holds: Optional[int]) -> None:
        """y columns for a player worth `value` on day d: they start at most once, and a spot's
        player only on a day they hold one (`holds`, their x column)."""
        units = round(value / UNIT)
        ys = []
        for s in sorted(eligible) if units > 0 else ():
            if s in seats:
                y = m.col()
                points[y] = units
                seated[(s, d)].append(y)
                ys.append(y)
        if ys and holds is None:
            m.row(-inf, 1.0, ys, [1.0] * len(ys))
        elif ys:
            m.row(-inf, 0.0, [*ys, holds], [*([1.0] * len(ys)), -1.0])

    for d in range(horizon):
        for r in rest_by_day[d]:
            seat(r.value, r.eligible, d, None)
        for j, p in enumerate(players):
            if j < spots or d >= p.available_from:
                seat(p.values[d], p.eligible, d, x[j][d])
        m.row(spots, spots, [x[j][d] for j in range(n)], [1.0] * n)
        if day_caps[d] < spots:
            m.row(-inf, day_caps[d], [a[j][d] for j in range(n)], [1.0] * n)
    for (s, d), ys in seated.items():
        if len(ys) > seats[s]:
            m.row(-inf, seats[s], ys, [1.0] * len(ys))
    for j in range(n):
        held_before = j < spots
        for d in range(horizon):
            if d > 0:
                m.row(-inf, 0.0, [x[j][d], x[j][d - 1], a[j][d]], [1.0, -1.0, -1.0])
            elif not held_before:
                m.row(-inf, 0.0, [x[j][0], a[j][0]], [1.0, -1.0])
            for t in range(d + 1, min(horizon, d + rehold_after)):
                # Dropped on day d (held the day before, not on d): not back on day t.
                if d > 0:
                    m.row(-inf, 1.0, [x[j][d - 1], x[j][d], x[j][t]], [1.0, -1.0, 1.0])
                elif held_before:
                    m.row(-inf, 0.0, [x[j][t], x[j][0]], [1.0, -1.0])
    every_add = [a[j][d] for j in range(n) for d in range(horizon)]
    if adds is not None and most_adds < sum(day_caps):
        m.row(-inf, most_adds, every_add, [1.0] * len(every_add))
    for pos, days in room.items():
        at = [j for j, p in enumerate(players) if p.position == pos]
        held = sum(1 for h in holders if h.position == pos)
        for d, r in enumerate(days):
            if at and max(r, held) < len(at):
                m.row(-inf, max(r, held), [x[j][d] for j in at], [1.0] * len(at))

    # Points, then fewer adds, then fewer of them today: whole-number weights, each level worth
    # more than everything below it can add up to.
    today = min(spots, day_caps[0], most_adds) + 1
    cost = np.zeros(len(m.col_lower))
    for y, units in points.items():
        cost[y] = units * (most_adds + 1) * today
    for j in range(n):
        for d in range(horizon):
            cost[a[j][d]] = -(today + (1 if d == 0 else 0))

    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("threads", 1)                 # one thread: the same input gives the same plan
    h.setOptionValue("time_limit", float(time_limit))
    h.setOptionValue("mip_rel_gap", 0.0)
    h.setOptionValue("mip_abs_gap", 1 - 1e-6)      # the objective is whole: closer than 1 is proven
    lp = highspy.HighsLp()
    lp.num_col_ = len(m.col_lower)
    lp.num_row_ = len(m.row_lower)
    lp.col_cost_ = cost
    lp.col_lower_ = np.array(m.col_lower)
    lp.col_upper_ = np.array(m.col_upper)
    lp.row_lower_ = np.array(m.row_lower, dtype=float)
    lp.row_upper_ = np.array(m.row_upper, dtype=float)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
    lp.a_matrix_.start_ = np.array([*m.starts, len(m.index)], dtype=np.int32)
    lp.a_matrix_.index_ = np.array(m.index, dtype=np.int32)
    lp.a_matrix_.value_ = np.array(m.value, dtype=float)
    lp.sense_ = highspy.ObjSense.kMaximize
    integrality = [highspy.HighsVarType.kContinuous] * lp.num_col_
    for row in x:
        for col in row:
            integrality[col] = highspy.HighsVarType.kInteger
    lp.integrality_ = integrality
    h.passModel(lp)
    # Keeping every spot's player is always a plan: start from it, so there is one to return.
    keep = np.zeros(lp.num_col_)
    for j in range(spots):
        keep[x[j]] = 1.0
    start = highspy.HighsSolution()
    start.col_value = keep
    start.value_valid = True
    h.setSolution(start)
    h.run()

    status = h.getModelStatus()
    if h.getInfo().primal_solution_status != highspy.SolutionStatus.kSolutionStatusFeasible:
        return _plan(holders, players, [frozenset(holder_ids)] * horizon, rest_by_day, seats, optimal=False,
                     considered=considered)
    col = h.getSolution().col_value
    held = [frozenset(p.player_id for j, p in enumerate(players) if col[x[j][d]] > 0.5) for d in range(horizon)]
    return _plan(holders, players, held, rest_by_day, seats, optimal=status == highspy.HighsModelStatus.kOptimal,
                 considered=considered)


def _plan(holders: Sequence[Streamer], players: Sequence[Streamer], held: Sequence[frozenset[int]],
          rest_by_day: Sequence[Sequence[DayPlayer]], seats: Mapping[int, int], *, optimal: bool,
          considered: frozenset[int]) -> MultiStreamPlan:
    """The plan from who holds a spot each day, its values recomputed from the lineups. A spot whose
    player leaves takes one who arrives that day at the same position when there is one, then the
    lowest id first. ESPN checks its position limits switch by switch, in whatever order a day's are
    made: paired like for like, the day's other switches never both add to a position and drop from
    it, so no position holds more partway through the day than the plan leaves it with."""
    by_id = {p.player_id: p for p in players}
    bases = [best_lineup_value(rest, seats) for rest in rest_by_day]

    def worth(d: int, ids) -> float:
        """What the players `ids` add to day d's best lineup together."""
        mine = [DayPlayer(pid, by_id[pid].values[d], by_id[pid].eligible) for pid in ids]
        return max(0.0, best_lineup_value([*rest_by_day[d], *mine], seats) - bases[d]) if mine else 0.0

    now = [h.player_id for h in holders]
    rows: list[tuple[int, ...]] = []
    switches: list[Switch] = []
    for d, ids in enumerate(held):
        leaving = [k for k, pid in enumerate(now) if pid not in ids]
        arriving = sorted(ids - set(now))
        takes: dict[int, int] = {}
        for k in leaving:
            same = next((pid for pid in arriving if by_id[pid].position == by_id[now[k]].position), None)
            if same is not None:
                takes[k] = same
                arriving.remove(same)
        takes.update(zip((k for k in leaving if k not in takes), arriving, strict=True))
        for k in leaving:
            switches.append(Switch(d, k, now[k], takes[k]))
            now[k] = takes[k]
        rows.append(tuple(now))
    day_values = [worth(d, ids) for d, ids in enumerate(held)]
    runs_by_spot: list[tuple[Stretch, ...]] = []
    marginals: list[tuple[float, ...]] = []
    hold_values: list[float] = []
    for k, h in enumerate(holders):
        runs: list[Stretch] = []
        mine: list[float] = []
        hold = 0.0
        for d, row in enumerate(rows):
            pid = row[k]
            others = [other for other in row if other != pid]
            without = worth(d, others)
            value = day_values[d] - without
            mine.append(value)
            hold += value if pid == h.player_id else worth(d, {*others, h.player_id}) - without
            if runs and runs[-1].player_id == pid:
                last = runs[-1]
                runs[-1] = Stretch(pid, last.start, d + 1, last.value + value)
            else:
                runs.append(Stretch(pid, d, d + 1, value))
        runs_by_spot.append(tuple(runs))
        marginals.append(tuple(mine))
        hold_values.append(hold)
    holder_ids = tuple(h.player_id for h in holders)
    return MultiStreamPlan(
        holder_ids=holder_ids,
        spots=tuple(tuple(row[k] for row in rows) for k in range(len(holders))),
        stretches=tuple(runs_by_spot),
        day_values=tuple(day_values),
        value=sum(day_values),
        hold_value=sum(worth(d, holder_ids) for d in range(len(rest_by_day))),
        adds=len(switches),
        switches=tuple(switches),
        optimal=optimal,
        marginals=tuple(marginals),
        hold_values=tuple(hold_values),
        considered=considered,
    )
