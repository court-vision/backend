"""
The chance a matchup is won, guessed from the matchup's own numbers.

The league projection (`services.league_projection`) already gives an ESPN points
league's current matchup a win chance: every roster seated day by day under the
league's scoring. This is the best guess for the matchups it doesn't reach (a
categories league, a provider it doesn't read, a league still being projected), so
that every team on the Home desk can be coloured and ordered by how its week is
going. Its inputs are what a matchup read already holds: the totals so far and
today's projection (each counting player's per-game line times his games left).

**Points.** The league projection's own noise on the matchup's two numbers: the
score so far is fixed, and each side's points still to come spread as
`league_projection.win_chance` spreads them.

**Categories.** Each category is its own race. A counting category's total still to
come is a sum over many games, so its variance grows with it: `DISPERSION` is the
variance per unit still to come (points arrive in bigger lumps than steals), plus a
share of what's left for what the projection can't know (`PROJECTION_SD`: who sits,
who plays more). A shooting rate is a proportion of the attempts still to come:
binomial noise on those, diluted by the attempts already taken. Each race gives the
chance it is won, and the week is won by taking more categories than the opponent,
a level week counting half. The races are counted as independent, which they aren't
quite: one big night moves several at once.

Not calibrated against results: the constants are the spreads of typical box-score
lines. Good enough to tell a coin flip from a settled week, which is what it is for.

Pure: no database, no provider, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from services.league_projection import win_chance as points_win_chance

# Variance per unit of a counting category still to come: a player's game-to-game variance
# over his mean (25 points come 8 or so either way; a steal comes one at a time)
DISPERSION = {
    "pts": 4.5, "reb": 2.0, "oreb": 1.6, "dreb": 1.8, "ast": 1.8, "stl": 1.1, "blk": 1.3, "tov": 1.0,
    "fg3m": 1.3, "fgm": 2.0, "ftm": 2.0, "fga": 1.5, "fta": 2.0, "fg3a": 1.5, "pf": 0.8, "min": 3.0,
}
DEFAULT_DISPERSION = 1.5
# The projection's own error on what's left, as a share of it
PROJECTION_SD = 0.10
# A roster's attempts in a typical week, for a rate whose attempts so far aren't known
TYPICAL_ATTEMPTS = {"fg_pct": 420.0, "ft_pct": 110.0, "fg3_pct": 150.0}
# A roster shoots at many rates, not one: wider than a single binomial
RATE_SPREAD = 1.25
# A rate that isn't a proportion of attempts (assists per turnover): its spread over a whole week, as a share of it
OTHER_RATE_SD = 0.12
# A win chance within this of 0 or 1 is a week all but settled
DECIDED = 0.03


@dataclass(frozen=True)
class CategoryRace:
    """One category of a matchup: where both sides stand and where they are projected to end."""

    key: str
    higher_is_better: bool
    is_rate: bool
    now: tuple[float, float]                       # (yours, theirs) so far
    final: tuple[float, float]                     # projected at the matchup's end
    # A rate's attempts so far, when the read carries them
    attempts: tuple[Optional[float], Optional[float]] = (None, None)


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _ahead(diff: float, var: float, higher_is_better: bool) -> float:
    """The chance a lead of `diff` holds under noise of variance `var`."""
    if not higher_is_better:
        diff = -diff
    if var <= 0:
        return 1.0 if diff > 0 else (0.0 if diff < 0 else 0.5)
    return _phi(diff / math.sqrt(var))


def points_chance(your_score: float, opponent_score: float, your_projected: float,
                  opponent_projected: float) -> Optional[float]:
    """A points matchup from its score and both projected finals. None when nothing is
    scored or projected yet (no numbers to guess from)."""
    finals = (max(your_projected, your_score), max(opponent_projected, opponent_score))
    typical = sum(finals) / 2
    if typical <= 0:
        return None
    return points_win_chance(your_score, finals[0] - your_score, opponent_score, finals[1] - opponent_score, typical)


def share_left(races: Sequence[CategoryRace], side: int) -> float:
    """How much of a side's week is still to come: the middle of its counting categories'
    shares still to score. Half when no counting category says."""
    shares = sorted(min(max((r.final[side] - r.now[side]) / r.final[side], 0.0), 1.0)
                    for r in races if not r.is_rate and r.final[side] > 0)
    if not shares:
        return 0.5
    mid = len(shares) // 2
    return shares[mid] if len(shares) % 2 else (shares[mid - 1] + shares[mid]) / 2


def category_chance(race: CategoryRace, left: tuple[float, float]) -> float:
    """The chance one category is won. `left`: each side's share of the week still to come."""
    if not race.is_rate:
        finals, var = [], 0.0
        for side in (0, 1):
            final = max(race.final[side], race.now[side])
            to_come = final - race.now[side]
            finals.append(final)
            var += DISPERSION.get(race.key, DEFAULT_DISPERSION) * to_come + (PROJECTION_SD * to_come) ** 2
        return _ahead(finals[0] - finals[1], var, race.higher_is_better)
    var = 0.0
    for side in (0, 1):
        share = min(max(left[side], 0.0), 1.0)
        if race.key not in TYPICAL_ATTEMPTS:
            var += (OTHER_RATE_SD * abs(race.final[side])) ** 2 * share
            continue
        rate = min(max(race.final[side], 0.0), 1.0)
        taken = race.attempts[side]
        # the week's attempts: what's been taken scaled up by the share left, once enough has been played to scale
        total = taken / (1 - share) if taken and share < 0.85 else TYPICAL_ATTEMPTS[race.key]
        var += RATE_SPREAD * rate * (1 - rate) * (total * share) / total ** 2
    return _ahead(race.final[0] - race.final[1], var, race.higher_is_better)


def week_chance(chances: Sequence[float]) -> Optional[float]:
    """The chance of taking more categories than the opponent, a level week counting half.
    None without a category."""
    if not chances:
        return None
    won = [1.0]                                    # won[k]: the chance of exactly k categories so far
    for p in chances:
        nxt = [0.0] * (len(won) + 1)
        for k, q in enumerate(won):
            nxt[k] += q * (1 - p)
            nxt[k + 1] += q * p
        won = nxt
    n = len(chances)
    level = won[n // 2] if n % 2 == 0 else 0.0
    return min(max(sum(q for k, q in enumerate(won) if 2 * k > n) + level / 2, 0.0), 1.0)


def categories_chance(races: Sequence[CategoryRace]) -> Optional[float]:
    """A categories matchup from its races. None without one."""
    left = (share_left(races, 0), share_left(races, 1))
    return week_chance([category_chance(r, left) for r in races])


def closeness(win_chance: Optional[float]) -> float:
    """How much a week is still in play: 1 at a coin flip, 0 once it is settled. A week with no
    win chance counts as half."""
    if win_chance is None:
        return 0.5
    return 1 - abs(2 * min(max(win_chance, 0.0), 1.0) - 1)


def decided(win_chance: Optional[float]) -> bool:
    """The week is all but settled, either way."""
    return win_chance is not None and (win_chance <= DECIDED or win_chance >= 1 - DECIDED)
