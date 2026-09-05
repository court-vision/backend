"""
Category fit: what a candidate is worth to *this* roster, next to what he is
worth to any roster.

The board's `value` is balanced — a player's summed per-category z mapped onto
the fantasy-points scale (services.scoring.category_value). That is the right
number for a big board and the wrong one for pick 34 of a draft where you
already have four guards and no blocks. Fit re-weights the same z's by how far
this roster trails an average team, category by category, and by which
categories the manager has decided to concede.

The model, in four steps:

1. **Pace**, from real teams wherever there are real teams to read. Every pick
   records the seat that made it, so the opposing rosters sum directly: pace is
   the mean of what those teams actually hold in category c, each scaled to my
   own pick count so a seat one pick ahead in the snake does not read as a
   lead. Before enough seats have drafted anybody — and in a room with no seats
   at all — it falls back to the original estimate: the draftable tier, the top
   `league_size x roster_size` players by balanced score over the full pool,
   whose mean z per category times k is what an average team would hold.
   `FitModel.pace_source` says which was used; the two are the same shape, so
   nothing downstream changes with it.
2. **Need.** `need_c = (pace_c - mine_c) / spread_c`, clamped to +/-2. The
   spread is the observed dispersion across the opposing teams when they are
   what is being read, and `stdev(z_c over the tier) x sqrt(k)` — the spread of
   a k-pick sum — when they are not. It reads directly: "1.2 standard deviations
   behind pace in assists". Still no probabilities: a calibrated one needs an
   ADP *distribution*, which no source publishes (plan diff #6).
3. **Weights.** `w_c = 0` for a punted category, else `1 + FIT_GAIN x need_c`.
   With FIT_GAIN at 0.25 and the clamp at 2, weights live in [0.5, 1.5]: fit
   leans a ranking, it never inverts a category's sign.
4. **Value.** Fit is the balanced z-sum plus what the weights move it by:
   `fit_z = balanced + sum((w_c - 1) x z_c)`, mapped through the same
   `category_value` as the balanced score, so the two columns share a scale and
   their difference is meaningful. Written as a shift rather than as
   `sum(w_c x z_c)` on purpose: the two are the same number in arithmetic but
   not in floating point, and an all-ones model must return the balanced value
   *exactly*, or an untouched board would show a fit column differing from its
   value column by a rounding step and mean nothing by it.

`my_rank` per category comes from the same seat holdings, unscaled: where this
roster stands right now among the teams in the draft. That is the number a
manager means by "I am third in blocks and last in free-throw percentage", and
it is the input a punt *recommendation* would be built from.

Before the first pick k is 0, pace and holdings are both 0, so every need is 0
and fit is exactly balanced — until a punt is set, which is the point of
punting early. Everything here is pure: no database, no ORM, no I/O.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from math import sqrt
from typing import Collection, Iterable, Mapping, Optional, Sequence

from services.scoring.models import CategoryDef
from services.scoring.vocab import label_for

# How hard a category's need leans its weight. Small on purpose: at the +/-2
# clamp a category is worth 1.5x or 0.5x, never 0x (that is what punting is)
# and never negative (that would make a good line a bad one).
FIT_GAIN = 0.25
NEED_CLAMP = 2.0

# A tier of one has no spread to measure, and a spread of zero has no scale to
# divide by — both mean "no pace to be behind", i.e. need 0.
MIN_TIER = 2
SPREAD_EPS = 1e-9

# Draftable roster spots when a room knows neither its rounds nor its slots.
DEFAULT_ROSTER_SIZE = 13

# Opposing seats that must have drafted somebody before their rosters are worth
# pacing against. Two teams are an anecdote and the dispersion across them is
# noise; below this the tier estimate is the better answer.
MIN_SEATS_FOR_PACE = 3

NEED_DECIMALS = 3
VALUE_DECIMALS = 3


@dataclass(frozen=True)
class CategoryNeed:
    """One category's standing on this roster, and what fit does about it."""

    key: str
    label: str
    mine: float         # summed z this roster holds in the category
    pace: float         # what an average team holds after the same number of picks
    spread: float       # standard deviation of that k-pick sum over the tier
    need: float         # (pace - mine) / spread, clamped: + means behind pace
    weight: float       # 0 when punted, else 1 + FIT_GAIN * need
    punted: bool
    # Where this roster stands when the other seats can be read: 1 = holds the
    # most of this category of any team in the draft. None when they cannot.
    my_rank: Optional[int] = None
    seats: Optional[int] = None


@dataclass(frozen=True)
class FitModel:
    """The weights one room's board is scored with, plus what produced them."""

    needs: tuple[CategoryNeed, ...]
    picks_counted: int      # my roster players the pool could score (see build_fit_model)
    tier_size: int
    pace_source: str = "tier"   # seats | tier
    seats_drafted: int = 0      # opposing seats holding somebody the pool can score

    @property
    def weights(self) -> dict[str, float]:
        return {n.key: n.weight for n in self.needs}

    @property
    def punts(self) -> list[str]:
        return [n.key for n in self.needs if n.punted]

    def shift(self, z: Optional[Mapping[str, float]]) -> float:
        """How far this roster's weights move a candidate off his balanced z-sum.

        Exactly 0.0 for a model that weighs nothing — an undrafted, unpunted
        room — so fit and balanced open the draft identical rather than a
        rounding step apart.
        """
        if not z:
            return 0.0
        return sum((n.weight - 1.0) * float(z.get(n.key, 0.0)) for n in self.needs)

    def fit_z(self, balanced: float, z: Optional[Mapping[str, float]]) -> float:
        """The candidate's balanced z-sum, re-weighted for this roster."""
        return float(balanced) + self.shift(z)

    def drivers(self, z: Optional[Mapping[str, float]]) -> list[tuple[CategoryNeed, float]]:
        """Per category, how far it shifted the candidate off balanced, in z.

        `(w_c - 1) x z_c` — negative for a punted category the candidate is
        good at, positive for a category this roster needs. Largest move first,
        so a caller can name the two that decided the pick, and they sum to
        `shift`.
        """
        if not z:
            return []
        shifts = [(n, (n.weight - 1.0) * float(z.get(n.key, 0.0))) for n in self.needs]
        moved = [(n, shift) for n, shift in shifts if abs(shift) > SPREAD_EPS]
        moved.sort(key=lambda pair: -abs(pair[1]))
        return moved


def _clamp(x: float, limit: float = NEED_CLAMP) -> float:
    return max(-limit, min(limit, x))


def normalize_punts(
    punts: Iterable[str], allowed: Iterable[str]
) -> tuple[list[str], list[str]]:
    """(kept, unknown) — deduped and lower-cased, in the order given.

    Returns rather than raises: this module is pure, and only the caller knows
    whether an unknown key is a 400 or something to ignore.
    """
    permitted = {str(k).strip().lower() for k in allowed}
    kept: list[str] = []
    unknown: list[str] = []
    for raw in punts or ():
        key = str(raw).strip().lower()
        if not key or key in kept or key in unknown:
            continue
        (kept if key in permitted else unknown).append(key)
    return kept, unknown


def draftable_tier_size(
    league_size: Optional[int], roster_size: Optional[int]
) -> int:
    """How many players a whole league drafts — the pool fit paces against.

    Zero when the room does not know its own size: without a league there is no
    average team to be behind, and `build_fit_model` answers with a flat model.
    """
    if not league_size or league_size < 1:
        return 0
    return int(league_size) * int(roster_size or DEFAULT_ROSTER_SIZE)


def build_fit_model(
    ranked_z: Sequence[tuple[int, Optional[Mapping[str, float]]]],
    my_ids: Collection[int],
    categories: Sequence[CategoryDef],
    tier_size: int,
    punts: Iterable[str] = (),
    opponent_rosters: Optional[Sequence[Collection[int]]] = None,
) -> FitModel:
    """Weights for one roster, from the pool and the teams it is drafting against.

    `ranked_z` is every player in the pool as `(player_id, per-category z)`, in
    balanced-score order (best first) — exactly what `compute_category_scores`
    returns. `opponent_rosters` is one collection of player ids per opposing
    seat; pass none (or too few to measure) and the pace falls back to the tier
    estimate, which is what a room with no seats gets.

    Only players the pool can score are counted, on every side of the
    comparison: a drafted rookie with no stat line to z-score is absent from
    `mine`, from the opposing holdings, and from the pick counts they are
    scaled by — otherwise a roster would look behind in every category for
    owning him.
    """
    punted, _ = normalize_punts(punts, [c.key for c in categories])
    z_by_id = {pid: z for pid, z in ranked_z if z}
    tier = list(ranked_z[:tier_size]) if tier_size > 0 else []
    owned = [z_by_id[pid] for pid in my_ids if pid in z_by_id]
    picks = len(owned)

    # One entry per opposing seat that has drafted somebody the pool can score.
    seats = [
        scored for scored in (
            [z_by_id[pid] for pid in roster if pid in z_by_id]
            for roster in (opponent_rosters or ())
        )
        if scored
    ]
    # With nobody drafted there is nothing to be behind, whatever the seats
    # hold — the k = 0 case has to stay exactly balanced.
    use_seats = len(seats) >= MIN_SEATS_FOR_PACE and picks > 0

    needs: list[CategoryNeed] = []
    for category in categories:
        key = category.key
        mine = sum(float(z.get(key, 0.0)) for z in owned)
        held = [sum(float(z.get(key, 0.0)) for z in seat) for seat in seats]

        # The estimate, always computed: it is the pace when there are no seats
        # to read, and the yardstick when the seats agree too closely to be one.
        column = [float((z or {}).get(key, 0.0)) for _, z in tier]
        mean = statistics.fmean(column) if column else 0.0
        stdev = statistics.pstdev(column, mean) if len(column) >= MIN_TIER else 0.0
        tier_pace = mean * picks
        tier_spread = stdev * sqrt(max(picks, 1))

        if use_seats:
            # Each seat scaled to my pick count: a team one pick ahead in the
            # snake holds more of everything, and that is not a lead.
            scaled = [total * (picks / len(seat)) for total, seat in zip(held, seats)]
            pace = statistics.fmean(scaled)
            observed = statistics.pstdev(scaled, pace) if len(scaled) >= MIN_TIER else 0.0
            # Opponents in perfect agreement have no dispersion to divide by,
            # and that is the strongest statement about where the field sits,
            # not the absence of one. Keep what they actually hold as the pace
            # and borrow the pool's spread as the yardstick, rather than
            # reporting no signal in the one case the signal is unanimous.
            spread = observed if observed > SPREAD_EPS else tier_spread
        else:
            pace, spread = tier_pace, tier_spread

        need = _clamp((pace - mine) / spread) if spread > SPREAD_EPS else 0.0
        is_punted = key in punted

        needs.append(CategoryNeed(
            key=key,
            label=category.label or label_for(key),
            mine=round(mine, VALUE_DECIMALS),
            pace=round(pace, VALUE_DECIMALS),
            spread=round(spread, VALUE_DECIMALS),
            need=round(need, NEED_DECIMALS),
            weight=0.0 if is_punted else round(1.0 + FIT_GAIN * need, NEED_DECIMALS),
            punted=is_punted,
            # Standing is unscaled: what is on the rosters right now, which is
            # what "third in blocks" means. Ties share the better rank.
            my_rank=(1 + sum(1 for total in held if total > mine)) if held else None,
            seats=(len(held) + 1) if held else None,
        ))

    return FitModel(
        needs=tuple(needs), picks_counted=picks, tier_size=len(tier),
        pace_source=("seats" if use_seats else "tier"), seats_drafted=len(seats),
    )
