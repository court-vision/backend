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

1. **Pace.** The draftable tier is the top `league_size x roster_size` players
   by balanced score over the full pool — a fixed reference, drafted players
   included, so it does not drift as the board empties. An average team's
   holding in category c after k picks is `mean(z_c over the tier) x k`.
2. **Need.** `need_c = (pace_c - mine_c) / spread_c`, clamped to +/-2, where
   `spread_c = stdev(z_c over the tier) x sqrt(k)` — the spread of a k-pick
   sum. It reads directly: "1.2 standard deviations behind pace in assists".
   No probabilities: a calibrated one needs an ADP *distribution* and a model
   of the other seats, neither of which exists this season (plan diff #6).
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


@dataclass(frozen=True)
class FitModel:
    """The weights one room's board is scored with, plus what produced them."""

    needs: tuple[CategoryNeed, ...]
    picks_counted: int      # my roster players the pool could score (see build_fit_model)
    tier_size: int

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
) -> FitModel:
    """Weights for one roster, from the pool it is drafting out of.

    `ranked_z` is every player in the pool as `(player_id, per-category z)`, in
    balanced-score order (best first) — exactly what `compute_category_scores`
    returns. Only roster players the pool can score are counted, on both sides
    of the comparison: a drafted rookie with no stat line to z-score is absent
    from `mine` and must not be paced against either, or a roster would look
    behind in every category for owning him.
    """
    punted, _ = normalize_punts(punts, [c.key for c in categories])
    tier = list(ranked_z[:tier_size]) if tier_size > 0 else []
    owned = [z for pid, z in ranked_z if pid in my_ids and z]
    picks = len(owned)

    needs: list[CategoryNeed] = []
    for category in categories:
        key = category.key
        column = [float((z or {}).get(key, 0.0)) for _, z in tier]
        mean = statistics.fmean(column) if column else 0.0
        stdev = statistics.pstdev(column, mean) if len(column) >= MIN_TIER else 0.0

        pace = mean * picks
        mine = sum(float(z.get(key, 0.0)) for z in owned)
        spread = stdev * sqrt(max(picks, 1))
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
        ))

    return FitModel(needs=tuple(needs), picks_counted=picks, tier_size=len(tier))
