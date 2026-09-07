"""Draft Recap: what a finished draft was worth, seat by seat.

Pure arithmetic over picks that are already recorded — no database, no ESPN.
A room can be filled four ways (by hand, from a live ESPN room, from an import,
or by the autopicker) and all four land in `usr.draft_picks`, so the recap reads
one shape and never asks where the picks came from.

Three questions, in the order the page asks them:

- **What was each pick worth?** `value_over_slot` prices a pick against the board
  itself — the player CV ranked at that overall pick number. Positive means the
  seat beat the slot it was drafting from. `surplus_cv` and `surplus_market` say
  the same thing in rank terms, one against our board and one against ESPN's ADP.
- **Who drafted well?** Seats are graded A–F on the sum of their picks'
  `value_over_slot`, ranked against the other seats in the same room. The grade
  is a claim about this league and nothing else, and the page says so.
- **Who is projected to win what?** Category leagues get each seat's per-category
  z-sum ranked into roto points, plus the head-to-head matrix those z-sums imply.
  Points leagues get projected season value.

**Ties take the average of the positions they occupy** (`pos = rank + (tied−1)/2`)
everywhere in this module. Roto points only sum to n(n+1)/2 under that rule, and
one convention across grades and standings beats two. It deliberately differs
from `draft_fit.CategoryNeed.my_rank`, which takes the better rank because it is
answering "where do I stand", not dividing a fixed pot.

The standings are a z-sum approximation of a roto finish, not a season
simulation: a seat's strength in a category is the sum of its players' z, which
is the number the room's category-need bars are built from all draft. Keeping
those two identical is worth more than the extra realism of summing stat lines —
that is what `standings_basis` on the response exists to admit.

Nothing here drops a pick. A pick whose player never resolved, or who is in the
market snapshot but has neither a projection nor a baseline to score, is carried
with `value=None`, left out of its seat's sum, and counted in `unscored`. A seat
is never quietly graded down for a gap in our data.
"""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from services.scoring.models import CategoryDef

# Relative letters, worst last. A four-seat league therefore tops out at D:
# with four teams an F is a claim the sample cannot carry.
GRADE_CURVE = ("A", "B", "C", "D", "F")
VALUE_DECIMALS = 2
Z_DECIMALS = 3


@dataclass(frozen=True)
class RecapPick:
    """One recorded pick, as the recap reads it — `usr.draft_picks` minus the
    columns the arithmetic has no use for."""

    overall_pick: int
    round: Optional[int] = None
    slot: Optional[int] = None
    by_me: bool = False
    source: str = "manual"
    player_id: Optional[int] = None
    espn_player_id: Optional[int] = None
    espn_team_id: Optional[int] = None
    player_name: Optional[str] = None
    bid: Optional[float] = None


@dataclass(frozen=True)
class ScoredPick:
    pick: RecapPick
    value: Optional[float] = None
    cv_rank: Optional[int] = None
    market_rank: Optional[int] = None
    adp: Optional[float] = None
    surplus_cv: Optional[int] = None
    surplus_market: Optional[float] = None
    value_over_slot: Optional[float] = None


@dataclass(frozen=True)
class SeatSummary:
    slot: int
    espn_team_id: Optional[int]
    is_me: bool
    picks: int
    unscored: int
    total_value: float
    value_over_slot: Optional[float]
    grade: Optional[str]
    position: Optional[float]
    best_pick: Optional[int]
    worst_pick: Optional[int]


@dataclass(frozen=True)
class CategoryLine:
    key: str
    label: str
    z_sum: float
    rank: float
    roto_points: float


@dataclass(frozen=True)
class H2HCell:
    opponent_slot: int
    won: float
    lost: float
    tied: float


@dataclass(frozen=True)
class SeatStanding:
    slot: int
    categories: list[CategoryLine] = field(default_factory=list)
    roto_points: Optional[float] = None
    roto_rank: Optional[float] = None
    season_value: Optional[float] = None
    value_rank: Optional[float] = None
    expected_wins: Optional[float] = None
    h2h: list[H2HCell] = field(default_factory=list)


@dataclass(frozen=True)
class Recap:
    picks: list[ScoredPick]
    seats: list[SeatSummary]
    standings: list[SeatStanding]
    graded_by: str
    unscored: int
    unattributed: int


def positions_of(totals: Mapping[int, float]) -> dict[int, float]:
    """Key -> position, best (highest) first, ties sharing the average position.

    Twelve seats always spend 78 positions between them however they tie, which
    is what lets roto points come out of the same function as grades.
    """
    values = list(totals.values())
    out: dict[int, float] = {}
    for key, total in totals.items():
        better = sum(1 for other in values if other > total)
        tied = sum(1 for other in values if other == total)
        out[key] = better + 1 + (tied - 1) / 2
    return out


def grade_for(position: float, seats: int) -> Optional[str]:
    """A forced quintile curve over the seats in this room.

    A league where every seat ties lands them all on the same mid-field
    letter, which is the honest answer when no seat separated itself.
    """
    if seats <= 0:
        return None
    index = int((position - 1) * len(GRADE_CURVE) / seats)
    return GRADE_CURVE[min(len(GRADE_CURVE) - 1, max(0, index))]


def _sum(values: Sequence[float], decimals: int) -> float:
    """One rounding over the terms, never a sum of separately rounded sums."""
    return round(sum(values), decimals) or 0.0


def build_recap(
    picks: Sequence[RecapPick],
    ladder: Sequence[tuple[int, float]],
    market: Optional[Mapping[int, Mapping]] = None,
    *,
    is_categories: bool = False,
    categories: Sequence[CategoryDef] = (),
    category_z: Optional[Mapping[int, Mapping[str, float]]] = None,
    season_value: Optional[Mapping[int, float]] = None,
    draft_type: str = "snake",
    my_slot: Optional[int] = None,
) -> Recap:
    """Score every pick, grade every seat, and project the standings.

    `ladder` is the board in CV order — `(player_id, value)`, best first, the
    same order `cv_rank` enumerates — so the player ranked at pick *k* is
    `ladder[k - 1]` whether or not anybody drafted him.
    """
    market = market or {}
    category_z = category_z or {}
    season_value = season_value or {}

    cv_rank = {pid: rank for rank, (pid, _value) in enumerate(ladder, start=1)}
    value_of = {pid: value for pid, value in ladder}
    # An auction pick number is a nomination order, not a place in a value
    # ladder, so pricing one against the k-th best player would be arithmetic
    # about nothing. Those rooms grade on what they drafted instead.
    priced_by_slot = draft_type != "auction"

    scored: list[ScoredPick] = []
    for pick in sorted(picks, key=lambda p: p.overall_pick):
        pid = pick.player_id
        value = value_of.get(pid) if pid is not None else None
        rank = cv_rank.get(pid) if pid is not None else None
        row = market.get(pid, {}) if pid is not None else {}
        market_rank = row.get("overall_rank")
        adp = row.get("adp")
        slot_value = (
            ladder[pick.overall_pick - 1][1]
            if priced_by_slot and 0 < pick.overall_pick <= len(ladder)
            else None
        )
        scored.append(ScoredPick(
            pick=pick,
            value=value,
            cv_rank=rank,
            market_rank=market_rank,
            adp=adp,
            surplus_cv=(rank - pick.overall_pick) if rank is not None else None,
            surplus_market=(round(adp - pick.overall_pick, 1)) if adp is not None else None,
            value_over_slot=(
                round(value - slot_value, VALUE_DECIMALS)
                if value is not None and slot_value is not None else None
            ),
        ))

    seats = _seats(scored, my_slot=my_slot, priced_by_slot=priced_by_slot)
    standings = _standings(
        scored,
        [seat.slot for seat in seats],
        is_categories=is_categories,
        categories=categories,
        category_z=category_z,
        season_value=season_value,
    )
    return Recap(
        picks=scored,
        seats=seats,
        standings=standings,
        graded_by="value_over_slot" if priced_by_slot else "value",
        unscored=sum(1 for s in scored if s.value is None),
        unattributed=sum(1 for s in scored if s.pick.slot is None),
    )


def _seats(scored: Sequence[ScoredPick], *, my_slot: Optional[int], priced_by_slot: bool) -> list[SeatSummary]:
    """One summary per seat that made a pick, graded against the others.

    A pick with no seat (a room that never learned its pick order) is still
    listed among the picks; it just has no roster to join.
    """
    by_seat: dict[int, list[ScoredPick]] = {}
    for entry in scored:
        if entry.pick.slot is not None:
            by_seat.setdefault(int(entry.pick.slot), []).append(entry)
    if not by_seat:
        return []

    totals: dict[int, float] = {}
    for slot, entries in by_seat.items():
        graded = [e.value_over_slot for e in entries if e.value_over_slot is not None]
        values = [e.value for e in entries if e.value is not None]
        totals[slot] = _sum(graded, VALUE_DECIMALS) if priced_by_slot else _sum(values, VALUE_DECIMALS)

    places = positions_of(totals)
    count = len(by_seat)

    summaries: list[SeatSummary] = []
    for slot in sorted(by_seat):
        entries = by_seat[slot]
        graded = [e for e in entries if e.value_over_slot is not None]
        values = [e.value for e in entries if e.value is not None]
        best = max(graded, key=lambda e: e.value_over_slot, default=None)
        worst = min(graded, key=lambda e: e.value_over_slot, default=None)
        summaries.append(SeatSummary(
            slot=slot,
            espn_team_id=next((e.pick.espn_team_id for e in entries if e.pick.espn_team_id is not None), None),
            is_me=(slot == my_slot) if my_slot is not None else any(e.pick.by_me for e in entries),
            picks=len(entries),
            unscored=sum(1 for e in entries if e.value is None),
            total_value=_sum(values, VALUE_DECIMALS),
            value_over_slot=_sum([e.value_over_slot for e in graded], VALUE_DECIMALS) if priced_by_slot else None,
            grade=grade_for(places[slot], count),
            position=places[slot],
            best_pick=best.pick.overall_pick if best is not None else None,
            worst_pick=worst.pick.overall_pick if worst is not None else None,
        ))
    return summaries


def _standings(
    scored: Sequence[ScoredPick],
    slots: Sequence[int],
    *,
    is_categories: bool,
    categories: Sequence[CategoryDef],
    category_z: Mapping[int, Mapping[str, float]],
    season_value: Mapping[int, float],
) -> list[SeatStanding]:
    """Where the drafted rosters would finish, on the evidence of the draft."""
    if not slots:
        return []
    if not is_categories:
        totals = {
            slot: _sum(
                [season_value.get(e.pick.player_id, 0.0)
                 for e in scored if e.pick.slot == slot and e.pick.player_id is not None],
                VALUE_DECIMALS,
            )
            for slot in slots
        }
        places = positions_of(totals)
        return [SeatStanding(slot=slot, season_value=totals[slot], value_rank=places[slot]) for slot in slots]

    keys = [c.key for c in categories]
    held: dict[int, dict[str, float]] = {}
    for slot in slots:
        rosters = [
            category_z[e.pick.player_id]
            for e in scored
            if e.pick.slot == slot and e.pick.player_id is not None and e.pick.player_id in category_z
        ]
        held[slot] = {key: _sum([float(z.get(key, 0.0)) for z in rosters], Z_DECIMALS) for key in keys}

    per_category = {key: positions_of({slot: held[slot][key] for slot in slots}) for key in keys}
    count = len(slots)

    lines: dict[int, list[CategoryLine]] = {}
    roto: dict[int, float] = {}
    for slot in slots:
        rows = [
            CategoryLine(
                key=cat.key,
                label=cat.label,
                z_sum=held[slot][cat.key],
                rank=per_category[cat.key][slot],
                roto_points=round(count - per_category[cat.key][slot] + 1, 1),
            )
            for cat in categories
        ]
        lines[slot] = rows
        roto[slot] = _sum([row.roto_points for row in rows], 1)

    roto_places = positions_of(roto)
    return [
        SeatStanding(
            slot=slot,
            categories=lines[slot],
            roto_points=roto[slot],
            roto_rank=roto_places[slot],
            expected_wins=_expected_wins(slot, slots, held, keys),
            h2h=_h2h(slot, slots, held, keys),
        )
        for slot in slots
    ]


def _h2h(slot: int, slots: Sequence[int], held: Mapping[int, Mapping[str, float]], keys: Sequence[str]) -> list[H2HCell]:
    """This seat's line against every other seat: categories won, lost, tied."""
    cells = []
    for other in slots:
        if other == slot:
            continue
        won = sum(1 for key in keys if held[slot][key] > held[other][key])
        lost = sum(1 for key in keys if held[slot][key] < held[other][key])
        cells.append(H2HCell(opponent_slot=other, won=won, lost=lost, tied=len(keys) - won - lost))
    return cells


def _expected_wins(slot: int, slots: Sequence[int], held: Mapping[int, Mapping[str, float]], keys: Sequence[str]) -> Optional[float]:
    """Categories this seat wins in an average matchup — a tie counts half."""
    cells = _h2h(slot, slots, held, keys)
    if not cells:
        return None
    return round(sum(cell.won + cell.tied / 2 for cell in cells) / len(cells), 2)
