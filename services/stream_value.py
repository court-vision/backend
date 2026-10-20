"""
The value function for the stream planners (the CV AI plan, § 5): what a player is
worth per game, when he plays, under the question being asked.

The planners (`services.stream_planner`, `services.multi_stream_planner`) take one
scalar per player per day and never ask where it came from; the stream lanes feed
them the board's `avg_points`. This module is the other answers:

- `league`: the league's own measure, as the lanes value players today -- fantasy
  points under its weights, or the category value proxy in a categories league.
- a category (`blk`, ...): that category's per-game rate, for "who should I stream
  for blocks". Counting categories only: a rate (FG%, FT%) doesn't add up per
  player, and turnovers can't be streamed for on their own (every player adds
  some), so neither is offered.
- `matchup`: the category value proxy re-weighted toward the categories the
  projected matchup is close in, for "help me win this matchup": a category
  already won or lost by a mile weighs little, a dead heat weighs most.

The matchup weighting. Each counting category's projected margin is measured
against what one streamed spot can swing over the days left -- two pool standard
deviations of the per-game stat, times the games a spot plays -- and weighted

    w = 1 / (1 + (margin / swing)^2)

1 at a dead heat, 1/2 at one swing, 1/10 at three. The weights are renormalised to
sum to the number of categories, so a player's value is the category value proxy
(`services.scoring.category_value`: 25 for a pool-average player, 0 at a z-sum of
-5) with his z-scores reweighted: when every category is equally close it is the
proxy itself, and when one is in play it is mostly his standing in that one. With
nothing in play every player who plays is worth the pool average, and the planner
falls back to playing the most games. The shape is a judgment, not a measurement;
the eval is where it earns its keep.

Pure: no I/O. `services.ai.fantasy` loads the lines, the pool and the margins.
"""

from __future__ import annotations

import statistics
from typing import Mapping, Optional, Sequence

from services.scoring.category_rank import PoolRow
from services.scoring.category_value import category_value
from services.scoring.models import StatLine

LEAGUE, MATCHUP = "league", "matchup"
# Higher-is-better counting stats stored at every grain: what a spot can be streamed for.
STREAMABLE_CATEGORIES: tuple[str, ...] = ("pts", "reb", "ast", "stl", "blk", "fg3m")
VALUE_KINDS: tuple[str, ...] = (LEAGUE, MATCHUP, *STREAMABLE_CATEGORIES)
VALUE_LABELS: dict[str, str] = {
    LEAGUE: "value per game", MATCHUP: "matchup value per game",
    "pts": "points per game", "reb": "rebounds per game", "ast": "assists per game",
    "stl": "steals per game", "blk": "blocks per game", "fg3m": "threes per game",
}
_MIN_SWING = 1e-9


def category_rate(line: Optional[StatLine], key: str) -> float:
    """A player's per-game rate in one counting category; 0 with no line."""
    return max(0.0, line.get(key)) if line is not None else 0.0


def swing(pool_std: float, games: float) -> float:
    """What one streamed spot can swing a category by over `games`: two pool standard deviations of the
    per-game stat per game played (a below-average player out, an above-average one in)."""
    return max(2.0 * pool_std * games, _MIN_SWING)


def category_weight(margin: float, swing_: float) -> float:
    """1 at a dead heat, falling as the margin grows against the swing: 1/2 at one swing, 1/10 at three."""
    return 1.0 / (1.0 + (margin / max(swing_, _MIN_SWING)) ** 2)


def signed_margin(you: float, opp: float, higher_is_better: bool) -> float:
    """The projected margin, positive when you lead."""
    return (you - opp) if higher_is_better else (opp - you)


def pool_stds(pool: Sequence[PoolRow], keys: Sequence[str]) -> dict[str, float]:
    """Each category's population standard deviation of per-game values over the pool."""
    out: dict[str, float] = {}
    for key in keys:
        xs = [row.line.get(key) for row in pool]
        out[key] = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return out


def matchup_weights(margins: Mapping[str, float], stds: Mapping[str, float], games: float) -> dict[str, float]:
    """One weight per category in `margins` from its signed projected margin, `stds` the pool's per-game
    standard deviations and `games` what a streamed spot plays over the days left."""
    return {key: category_weight(margin, swing(stds.get(key, 0.0), games)) for key, margin in margins.items()}


def weighted_category_value(z: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """The category value proxy with the player's signed z-scores reweighted by `weights`, renormalised to
    sum to the number of categories; the pool-average value (25) when nothing is in play."""
    total = sum(weights.values())
    if not weights or total <= 0:
        return category_value(0.0)
    scale = len(weights) / total
    return category_value(sum(w * z.get(key, 0.0) for key, w in weights.items()) * scale)
