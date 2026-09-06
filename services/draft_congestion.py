"""
Slot congestion: what a candidate costs a roster on the nights it cannot start
everyone he plays alongside.

The hard position caps say who can be drafted. Under them, concentration still
costs: two more centres than centre-and-utility slots collide every night both
play, and four Nuggets share every Denver game night whatever their positions.
On those nights someone's value rides the bench. `season_value` counts a
player's games as if every one of them started; this term charges back the
ones that would not.

The model, in four steps:

1. **The sample.** A few ordinary fantasy weeks from the season's calendar —
   not the short opening week, not the merged All-Star fortnight — each day a
   set of the NBA teams playing. The board passes them in; this module reads
   no calendar itself.
2. **The lineup, per day.** The roster players whose team plays that day are
   matched against the league's active lineup slots (`roster_slots` minus
   bench and IR, UT open to everyone), eligibility from ESPN's own lineup slots.
   The matching is vertex-weighted by per-game value — process players best
   first, and admit each one whenever an augmenting path can still seat him.
   With the weights on one side of the graph the seatable sets form a
   transversal matroid, where that greedy is exact, and at fifteen players by
   thirteen slots it costs nothing. Unmatched player-games × per-game value is
   the day's benched value.
3. **The term.** For a candidate, `congestion = −(benched(roster + him) −
   benched(roster))`, the sample scaled to the season by
   `season_weeks / len(weeks)`. His own games are already in `season_value`, so
   this only ever charges for what his arrival benches — his or someone else's.
4. **Never positive.** Per day, the roster's optimal lineup is still a lineup
   once he is added, so the matched value cannot fall; and his lineup minus his
   own seat is a lineup for the roster alone, so the matched value cannot rise
   by more than his value. The benched value therefore moves by
   `v − (gain)` with `0 ≤ gain ≤ v`: between 0 and his value, never below 0.
   Summed over days and weeks, the term lives in `[−v × games × scale, 0]`.

Two assumptions, stated rather than hidden: today's per-game values hold all
season, and every calendar game is played (no games-played discount — the
term is a friction estimate, not a projection). Stacking needs no model of its
own: same team, same nights, and the matching sees the collision directly.

Missing data never penalizes on its own account. A player with no team on file
plays no sampled game and is left out of the matching; a candidate with no
team, a league with no known lineup slots, or a board with no sampled weeks
each read a reason instead of a number. Everything here is pure: no database,
no ORM, no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

# ---- lineup vocabulary --------------------------------------------------------
#
# Shared with the board service, which spreads a league's derived slots across
# the five primary positions for its starters-per-position count. It lives here
# because the matching needs it and the board imports this module, not the
# other way round.

# The five ESPN positions a player can be primary at, in default_position_id order.
ESPN_POSITIONS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")

# Lineup slots that hold a starter but are not a position: everyone is UT-eligible.
# The flexibility bonus ignores UT for that reason; the matching counts it, because
# a utility seat is still a seat.
UNIVERSAL_SLOTS = frozenset({"UT"})
NON_STARTING_SLOTS = frozenset({"BE", "IR", "Rookie", ""})

# Which ESPN positions can fill a multi-position lineup slot.
SLOT_MEMBERS: dict[str, tuple[str, ...]] = {
    "PG": ("PG",), "SG": ("SG",), "SF": ("SF",), "PF": ("PF",), "C": ("C",),
    "G": ("PG", "SG"), "F": ("SF", "PF"),
    "SG/SF": ("SG", "SF"), "G/F": ("PG", "SG", "SF", "PF"),
    "PF/C": ("PF", "C"), "F/C": ("SF", "PF", "C"),
    "UT": ESPN_POSITIONS,
}

VALUE_DECIMALS = 1

# What a sample is scaled to when the calendar cannot say how long the season is.
DEFAULT_SEASON_WEEKS = 24

# Roster players on one NBA team before the roster zone calls it a stack.
STACK_MIN = 2

# Why a candidate was not measured, verbatim in the component's detail.
NO_SLOTS = "no lineup slots known"
NO_SCHEDULE = "no schedule sampled"
NO_TEAM = "no team on file"


@dataclass(frozen=True)
class CongestionPlayer:
    """What the matching knows about one player."""

    id: int
    value: float                            # per game, floored at 0 by the caller
    team: Optional[str]                     # NBA tricode; None plays no sampled game
    slots: Optional[frozenset[str]] = None  # ESPN lineup slots he can start in; None = unknown
    position: Optional[str] = None          # ESPN primary, the fallback when slots are unknown


@dataclass(frozen=True)
class SampleWeek:
    """One fantasy week: per day, the teams with a game."""

    number: int
    days: tuple[frozenset[str], ...]


@dataclass(frozen=True)
class Stack:
    """Roster players sharing one team's schedule."""

    team: str
    count: int
    player_ids: tuple[int, ...]     # best first


@dataclass(frozen=True)
class Penalty:
    """One candidate's congestion, and the facts a detail line needs."""

    value: float                    # season scale, <= 0; exactly 0.0 when nothing is benched
    per_week: float                 # benched value his arrival adds per sampled week
    games: int                      # sampled days his team plays
    stack: int                      # roster players already on his team
    team: Optional[str]
    weeks: int                      # sampled weeks the number was measured over
    reason: Optional[str] = None    # set when he was not measured; `value` is then 0


@dataclass(frozen=True)
class CongestionModel:
    """One roster's lineup pressure over the sampled weeks."""

    slots: tuple[str, ...]                  # active slot instances, e.g. (... "UT", "UT", "UT")
    roster: tuple[CongestionPlayer, ...]    # the roster players in the matching (a team on file)
    no_team: tuple[int, ...]                # roster players left out of it
    weeks: tuple[SampleWeek, ...]
    season_weeks: int
    benched_sample: float                   # the roster's benched value over the sample, unscaled
    stacks: tuple[Stack, ...]

    @property
    def active(self) -> bool:
        """Whether there is anything to measure: slots to fill and nights to fill them."""
        return bool(self.slots) and bool(self.weeks)

    @property
    def scale(self) -> float:
        return self.season_weeks / len(self.weeks) if self.weeks else 0.0

    @property
    def sample_weeks(self) -> tuple[int, ...]:
        return tuple(week.number for week in self.weeks)

    @property
    def benched_per_week(self) -> float:
        if not self.weeks:
            return 0.0
        return round(self.benched_sample / len(self.weeks), VALUE_DECIMALS)

    @property
    def benched_season(self) -> float:
        return round(self.benched_sample * self.scale, VALUE_DECIMALS)

    def penalty(self, candidate: CongestionPlayer) -> Penalty:
        """What adding this candidate would bench, as a term that is never positive.

        The candidate is assumed not to be on the roster already; the board
        only asks about players still available.
        """
        weeks = len(self.weeks)
        stack = (
            sum(1 for p in self.roster if p.team == candidate.team) if candidate.team else 0
        )
        if not self.slots:
            return Penalty(0.0, 0.0, 0, stack, candidate.team, weeks, NO_SLOTS)
        if not self.weeks:
            return Penalty(0.0, 0.0, 0, stack, candidate.team, 0, NO_SCHEDULE)
        if not candidate.team:
            return Penalty(0.0, 0.0, 0, 0, None, weeks, NO_TEAM)

        games = sum(1 for week in self.weeks for day in week.days if candidate.team in day)
        with_him = benched_value(self.roster + (candidate,), self.slots, self.weeks)
        # >= 0 by the argument in the module docstring; max() absorbs float noise only.
        delta = max(0.0, with_him - self.benched_sample)
        # `or 0.0`: -round(0.0) is -0.0, and the room should never render that.
        value = -round(delta * self.scale, VALUE_DECIMALS) or 0.0
        return Penalty(
            value=value,
            per_week=round(delta / weeks, VALUE_DECIMALS),
            games=games,
            stack=stack,
            team=candidate.team,
            weeks=weeks,
        )


def active_slots(roster_slots: Optional[Mapping[str, object]]) -> tuple[str, ...]:
    """The league's starting slots as instances: a `{"UT": 3}` is three seats.

    Bench and IR hold no starter and are dropped; a count that is not a
    positive number is ignored, as the starters-per-position count does.
    """
    out: list[str] = []
    for slot, count in (roster_slots or {}).items():
        name = str(slot).strip()
        if not name or name in NON_STARTING_SLOTS:
            continue
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        out.extend([name] * max(n, 0))
    return tuple(out)


def eligible_slots(player: CongestionPlayer, active: Sequence[str]) -> frozenset[str]:
    """Which of the active slot names this player can start in.

    ESPN's lineup slots are authoritative when known — an empty intersection
    means he starts nowhere in this league. Without them, his primary position
    picks the slots that admit it; without that, every slot: missing data is
    never a penalty on its own account.
    """
    names = frozenset(active)
    if player.slots is not None:
        return names & player.slots
    if player.position:
        return frozenset(n for n in names if player.position in SLOT_MEMBERS.get(n, ()))
    return names


def week_from_calendar(number: int, game_span: int, games: Mapping[str, Mapping]) -> SampleWeek:
    """A calendar week (`{TRICODE: {"dayIdx": true}}`) as per-day team sets.

    Days run `0 .. game_span - 1`, so a merged 14-day week keeps all its days
    and nothing beyond the span is read.
    """
    days: list[frozenset[str]] = []
    for day in range(int(game_span)):
        key = str(day)
        days.append(frozenset(
            str(team) for team, played in (games or {}).items()
            if played and (played.get(key) or played.get(day))
        ))
    return SampleWeek(number=int(number), days=tuple(days))


def max_weight_assignment(
    players: Sequence[CongestionPlayer], slots: Sequence[str]
) -> frozenset[int]:
    """The ids that start when the lineup is chosen to maximize started value.

    Greedy by value with augmenting paths: each player, best first, is seated
    if some chain of re-seatings makes room for him. A successful chain never
    unseats anyone, so the started set only grows — which is exactly "take the
    heaviest element that keeps the set independent", optimal on a matroid.
    Ties break on id, so the answer is deterministic.
    """
    if not players or not slots:
        return frozenset()

    by_name: dict[str, list[int]] = {}
    for index, name in enumerate(slots):
        by_name.setdefault(name, []).append(index)
    seats: list[list[int]] = [
        [j for name in sorted(eligible_slots(p, slots)) for j in by_name.get(name, ())]
        for p in players
    ]
    owner: list[Optional[int]] = [None] * len(slots)

    def seat(i: int, seen: set[int]) -> bool:
        for j in seats[i]:
            if j in seen:
                continue
            seen.add(j)
            holder = owner[j]
            if holder is None or seat(holder, seen):
                owner[j] = i
                return True
        return False

    started: set[int] = set()
    order = sorted(range(len(players)), key=lambda i: (-players[i].value, players[i].id))
    for i in order:
        if seats[i] and seat(i, set()):
            started.add(players[i].id)
    return frozenset(started)


def benched_value(
    players: Sequence[CongestionPlayer], slots: Sequence[str], weeks: Sequence[SampleWeek]
) -> float:
    """Value that rides the bench over the sampled weeks, day by day."""
    total = 0.0
    for week in weeks:
        for teams_today in week.days:
            today = [p for p in players if p.team is not None and p.team in teams_today]
            if not today:
                continue
            started = max_weight_assignment(today, slots)
            total += sum(p.value for p in today if p.id not in started)
    return total


def build_congestion_model(
    roster: Sequence[CongestionPlayer],
    roster_slots: Optional[Mapping[str, object]],
    weeks: Sequence[SampleWeek],
    season_weeks: int = DEFAULT_SEASON_WEEKS,
) -> CongestionModel:
    """Measure one roster once; `penalty()` then answers for any candidate."""
    slots = active_slots(roster_slots)
    sampled = tuple(weeks)
    with_team = tuple(p for p in roster if p.team)
    no_team = tuple(p.id for p in roster if not p.team)
    benched = benched_value(with_team, slots, sampled) if slots and sampled else 0.0

    counts = Counter(p.team for p in with_team)
    stacks = tuple(
        Stack(
            team=team,
            count=count,
            player_ids=tuple(
                p.id for p in sorted(
                    (p for p in with_team if p.team == team), key=lambda p: (-p.value, p.id)
                )
            ),
        )
        for team, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= STACK_MIN
    )
    try:
        length = int(season_weeks)
    except (TypeError, ValueError):
        length = 0
    return CongestionModel(
        slots=slots,
        roster=with_team,
        no_team=no_team,
        weeks=sampled,
        season_weeks=length if length > 0 else DEFAULT_SEASON_WEEKS,
        benched_sample=benched,
        stacks=stacks,
    )
