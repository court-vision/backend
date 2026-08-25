"""
Category-league player rankings: per-category z-scores over a player pool.

Pure functions, no DB. Counting categories are z-scored on per-game values.
Rate categories (FG%, FT%, 3P%) are z-scored on *impact* -- the extra makes per
game a player contributes relative to a pool-average shooter at his own volume:

    impact = (player_pct - pool_pct) * player_attempts_per_game

where pool_pct is sum(makes) / sum(attempts) over the pool (never a mean of
percentages). A 55% shooter on 20 FGA/g therefore outranks a 60% shooter on
2 FGA/g, which is how percentage categories actually play in head-to-head.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from services.scoring.models import CategoryDef, StatLine
from services.scoring.vocab import STATS

# Rate stats whose makes/attempts are stored at every grain (rolling, season, live).
_RANKABLE_RATE_KEYS: tuple[str, ...] = ("fg_pct", "ft_pct", "fg3_pct")

# Stat keys that may be ranked: every stored counting stat except minutes, plus
# the rates that can be recomputed from stored makes/attempts.
RANKABLE_KEYS: tuple[str, ...] = tuple(
    key for key, d in STATS.items()
    if (not d.is_rate and key in StatLine.ROW_KEYS and key != "min") or key in _RANKABLE_RATE_KEYS
)

Z_DECIMALS = 3
VALUE_DECIMALS = 2
RATE_DECIMALS = 4


@dataclass
class PoolRow:
    """One player in the ranking pool. `line` holds per-game averages; for rate
    categories it carries per-game makes/attempts (fgm/fga, ftm/fta, ...)."""

    id: int
    name: str
    team: str | None
    gp: int
    line: StatLine
    fpts_avg: float
    fpts_total: float
    # Provider keys carried along so values can be looked up by ESPN id or by
    # normalized name (Yahoo rosters) without a second query.
    espn_id: int | None = None
    name_normalized: str | None = None


@dataclass
class ScoredRow:
    row: PoolRow
    # Display values: per-game counting values; rates as 0-1 fractions (None when no attempts).
    values: dict[str, float | None] = field(default_factory=dict)
    # Signed z-score per category (positive is always good, i.e. TOV is inverted).
    z: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


def _z_scores(xs: list[float]) -> list[float]:
    """Population z-scores; a degenerate pool (std == 0) scores everyone 0."""
    if not xs:
        return []
    mean = statistics.fmean(xs)
    std = statistics.pstdev(xs, mean)
    if std <= 1e-12:
        return [0.0] * len(xs)
    return [(x - mean) / std for x in xs]


def _clean(x: float, decimals: int) -> float:
    """Round and normalize -0.0 to 0.0 so JSON output never shows a negative zero."""
    return round(x, decimals) or 0.0


def compute_category_scores(pool: list[PoolRow], categories: list[CategoryDef]) -> list[ScoredRow]:
    """Score every pool row on each category and rank by total z (ties: fpts_avg desc).

    Raises ValueError for a category that is not in RANKABLE_KEYS.
    """
    for c in categories:
        if c.key not in RANKABLE_KEYS:
            raise ValueError(
                f"Unsupported ranking category '{c.key}'. Allowed: {', '.join(RANKABLE_KEYS)}"
            )

    scored = [ScoredRow(row=row) for row in pool]
    if not pool:
        return scored

    raw_scores = [0.0] * len(pool)
    for c in categories:
        d = STATS[c.key]
        inputs: list[float] = []
        if d.is_rate:
            makes = [row.line.get(d.numerator) for row in pool]
            attempts = [row.line.get(d.denominator) for row in pool]
            total_attempts = sum(attempts)
            pool_pct = sum(makes) / total_attempts if total_attempts > 0 else 0.0
            for s, m, a in zip(scored, makes, attempts):
                if a > 0:
                    pct = m / a
                    s.values[c.key] = _clean(pct, RATE_DECIMALS)
                    inputs.append((pct - pool_pct) * a)
                else:
                    s.values[c.key] = None
                    inputs.append(0.0)
        else:
            for s, row in zip(scored, pool):
                value = row.line.get(c.key)
                s.values[c.key] = _clean(value, VALUE_DECIMALS)
                inputs.append(value)

        sign = 1.0 if c.higher_is_better else -1.0
        for i, (s, z) in enumerate(zip(scored, _z_scores(inputs))):
            signed = sign * z
            s.z[c.key] = _clean(signed, Z_DECIMALS)
            raw_scores[i] += signed

    for s, total in zip(scored, raw_scores):
        s.score = _clean(total, Z_DECIMALS)

    order = sorted(range(len(scored)), key=lambda i: (-raw_scores[i], -scored[i].row.fpts_avg))
    return [scored[i] for i in order]
