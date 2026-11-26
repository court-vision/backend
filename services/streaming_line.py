"""
The streaming line (the Analysis desk plan, step 3): each of your players against what
the last stream lane worth running adds to your lineup, with your league's adds.

A lane streams one roster spot for a matchup (`services.multi_stream_planner`, the
planner the stream lanes run on). For one matchup the line is found lane by lane:

- With no lanes your team scores its best lineup each day, every player kept.
- With k lanes, your k weakest players hold them (weakest by what each adds to your
  lineup over the rest of the roster), and the planner streams the k spots together
  with the matchup's adds: who to add, on which day, and whom to keep. It may keep a
  spot's player, so a lane never costs points.
- The team's total with k lanes is the rest of the roster's best lineup each day plus
  what the lanes' players add to it. The best total sets how many lanes are worth
  running (the fewest within `MIN_GAIN` of the most), and the line is what the least of
  them adds to the lineup with the others in place: the last lane worth running.

Every lane count up to the adds is tried (up to `MAX_LANES`): the gains don't always
shrink lane by lane, because two streamers can compete for the same seats. A player
whose own worth to the lineup is under the line is one a lane would beat.

Adds reset every matchup, so the line is worked out matchup by matchup and summed over
a window, as each player's worth is.

What it can't see: it assumes today's free agents stay free and play as projected,
while other managers pick some up and some adds go to injuries. So the line runs a
little high, and `line_low` is the weakest lane of the plan re-made with one add fewer and
the free agent the plan gains most from gone: the bottom of its band.

Values are expected points: a player's points per game under the league's scoring at
his chance to play each game (the live projection's timeline, `league_projection`).
Lineups are ESPN's active slots by id. Position limits aren't modeled. A protected
player (`roster_protection`) is never moved into a lane; one on IR is not in the lineup.

Pure: no database, no ESPN, no clock. `services.streaming_line_service` reads the inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Sequence

from services import league_projection as lp
from services.multi_stream_planner import MultiStreamPlan, Streamer, plan_streams
from services.stream_planner import DayPlayer, best_lineup_value

# The most lanes tried for one matchup (the stream lanes allow four open at once)
MAX_LANES = 6
# A lane worth running adds at least this much to the matchup's total
MIN_GAIN = 1.0
DECIMALS = 1


@dataclass(frozen=True)
class LinePlayer:
    """One of your players: his projection, the active slots he may fill (ESPN ids), and
    whether he may hold a lane."""

    espn_id: int
    proj: lp.ProjPlayer
    slots: frozenset[int]
    protected: bool = False         # locked: never streamed
    on_ir: bool = False             # not in the lineup


@dataclass(frozen=True)
class LineFreeAgent:
    espn_id: int
    proj: lp.ProjPlayer
    slots: frozenset[int]
    available_on: Optional[date] = None     # waivers clear: the first day he can hold a spot


@dataclass(frozen=True)
class Lane:
    """The kth lane: the player who holds it now, what the plan with k lanes totals, and
    what the kth lane gains over k - 1."""

    lane: int
    holder: int                     # ESPN id
    total: float
    gain: float
    adds: int                       # adds the plan with k lanes makes
    optimal: bool


@dataclass(frozen=True)
class MatchupLine:
    days: int                       # days planned
    adds: Optional[int]             # adds the lanes could use (None: no limit)
    base: float                     # the team's total with no lanes
    lanes: tuple[Lane, ...]         # one per lane count tried, from 1
    worth_running: int              # lanes worth running
    line: Optional[float]           # what the last of them adds; None: no adds, or no one to add
    line_low: Optional[float]       # the bottom of the band (None: no line)
    worth: Mapping[int, float]      # ESPN id -> what he adds to your lineup over the rest of the roster


def day_value(p: lp.ProjPlayer, day: date, teams: frozenset[str]) -> float:
    """His expected points on `day`: per game at his chance to play, when his team plays."""
    if p.team is None or p.team not in teams:
        return 0.0
    return max(p.value, 0.0) * p.chance_on(day)


def _r(x: Optional[float]) -> Optional[float]:
    return round(float(x), DECIMALS) if x is not None else None


def _lineup(players: Sequence[DayPlayer], slots: Mapping[int, int]) -> float:
    return best_lineup_value([p for p in players if p.value > 0], slots)


def worth_to_lineup(players: Sequence[LinePlayer], days: Sequence[tuple[date, frozenset[str]]],
                    slots: Mapping[int, int]) -> dict[int, float]:
    """What each player adds to the lineup over the rest of the roster, summed over `days`:
    the best lineup with him less the best without him (what streaming his spot gives up)."""
    active = [p for p in players if not p.on_ir]
    out = {p.espn_id: 0.0 for p in players}
    for day, teams in days:
        row = [DayPlayer(p.espn_id, day_value(p.proj, day, teams), p.slots) for p in active]
        whole = _lineup(row, slots)
        for i, p in enumerate(active):
            if row[i].value > 0:
                out[p.espn_id] += whole - _lineup(row[:i] + row[i + 1:], slots)
    return out


def _streamers(players: Sequence[LinePlayer], days, first_day: date, *, holders: bool) -> list[Streamer]:
    out = []
    for p in players:
        values = tuple(day_value(p.proj, day, teams) for day, teams in days)
        start = 0
        if not holders and getattr(p, "available_on", None) is not None:
            start = max(0, (p.available_on - first_day).days)
            if start >= len(days):
                continue
        out.append(Streamer(p.espn_id, values, p.slots, start))
    return out


def _total(plan: MultiStreamPlan, rest: Sequence[LinePlayer], days, slots: Mapping[int, int]) -> float:
    base = sum(_lineup([DayPlayer(p.espn_id, day_value(p.proj, d, t), p.slots) for p in rest], slots)
               for d, t in days)
    return base + plan.value


def _weakest_lane(plan: MultiStreamPlan, rest: Sequence[LinePlayer], streamers: Mapping[int, Streamer],
                  days, slots: Mapping[int, int]) -> float:
    """What the plan's weakest spot adds to each day's lineup with the rest and the other spots in
    place: the lane worth least. (Which spot a player holds is the planner's labelling, so the
    last spot by number need not be the last one worth running.)"""
    worth = [0.0] * len(plan.spots)
    for d, (day, teams) in enumerate(days):
        row = [DayPlayer(p.espn_id, day_value(p.proj, day, teams), p.slots) for p in rest]
        held = [DayPlayer(s[d], streamers[s[d]].values[d], streamers[s[d]].eligible) for s in plan.spots]
        whole = _lineup(row + held, slots)
        for k in range(len(held)):
            worth[k] += whole - _lineup(row + held[:k] + held[k + 1:], slots)
    return min(worth)


def matchup_line(players: Sequence[LinePlayer], free_agents: Sequence[LineFreeAgent],
                 days: Sequence[tuple[date, frozenset[str]]], slots: Mapping[int, int], adds: Optional[int], *,
                 rehold_after: int = 2, max_lanes: int = MAX_LANES, time_limit: Optional[float] = None) -> MatchupLine:
    """The line for one matchup's `days` (from the first day a pickup can count for), with
    `adds` left (see the module docstring)."""
    worth = worth_to_lineup(players, days, slots)
    active = [p for p in players if not p.on_ir]
    base = sum(_lineup([DayPlayer(p.espn_id, day_value(p.proj, d, t), p.slots) for p in active], slots)
               for d, t in days)
    order = sorted((p for p in active if not p.protected), key=lambda p: (worth[p.espn_id], p.proj.value, p.espn_id))
    count = min(len(order), max_lanes, adds if adds is not None else max_lanes)
    if not days or count <= 0:
        return MatchupLine(len(days), adds, _r(base), (), 0, None, None, {k: _r(v) for k, v in worth.items()})

    first_day = days[0][0]
    on_roster = {p.espn_id for p in players}
    pool = _streamers([f for f in free_agents if f.espn_id not in on_roster], days, first_day, holders=False)
    if not pool:
        return MatchupLine(len(days), adds, _r(base), (), 0, None, None, {k: _r(v) for k, v in worth.items()})
    kw = {"time_limit": time_limit} if time_limit is not None else {}

    lanes: list[Lane] = []
    plans: list[tuple[MultiStreamPlan, list[LinePlayer]]] = []
    previous = base
    for k in range(1, count + 1):
        held = order[:k]
        rest = [p for p in active if p.espn_id not in {h.espn_id for h in held}]
        plan = plan_streams(_streamers(held, days, first_day, holders=True), pool,
                            [[DayPlayer(p.espn_id, day_value(p.proj, d, t), p.slots) for p in rest] for d, t in days],
                            slots, adds=adds, rehold_after=rehold_after, **kw)
        total = _total(plan, rest, days, slots)
        lanes.append(Lane(k, held[-1].espn_id, _r(total), _r(total - previous), plan.adds, plan.optimal))
        plans.append((plan, rest))
        previous = total

    totals = [base] + [lane.total for lane in lanes]
    best = max(totals)
    running = next(k for k, t in enumerate(totals) if t >= best - MIN_GAIN)
    at = max(running, 1)                    # with none worth running, the line is the first lane's worth
    plan, rest = plans[at - 1]
    streamers = {s.player_id: s for s in [*_streamers(order[:at], days, first_day, holders=True), *pool]}
    line = _weakest_lane(plan, rest, streamers, days, slots)

    line_low = line
    if plan.adds:
        # one add fewer, and the free agent the plan gains most from taken by someone else
        gains: dict[int, float] = {}
        for spot in plan.stretches:
            for s in spot:
                if s.player_id in {f.player_id for f in pool}:
                    gains[s.player_id] = gains.get(s.player_id, 0.0) + s.value
        taken = max(gains, key=gains.get) if gains else None
        thinner = [s for s in pool if s.player_id != taken]
        low = plan_streams(_streamers(order[:at], days, first_day, holders=True), thinner,
                           [[DayPlayer(p.espn_id, day_value(p.proj, d, t), p.slots) for p in rest] for d, t in days],
                           slots, adds=max(plan.adds - 1, 0), rehold_after=rehold_after, **kw)
        # the low plan's own weakest lane: charging the whole plan's loss to the weakest lane would
        # count a stronger lane's loss against it
        line_low = min(line, _weakest_lane(low, rest, streamers, days, slots))
    return MatchupLine(len(days), adds, _r(base), tuple(lanes), running, _r(line), _r(line_low),
                       {k: _r(v) for k, v in worth.items()})
