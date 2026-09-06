"""Research primitives only. Nothing here is imported by production services.

Arrays use raw makes/attempts; percentage team totals are always recomputed.
Normal probabilities and equal-remaining-pool completions are approximations.
"""
from dataclasses import dataclass
from datetime import datetime
from math import erf

import numpy as np

RAW = ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "min")
IX = {key: i for i, key in enumerate(RAW)}
CATS = ("fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov")
SIGN = np.array([1.] * 8 + [-1.])


def cdf(x):
    x = np.asarray(x, dtype=float)
    return .5 * (1 + np.fromiter((erf(v / np.sqrt(2)) for v in x.flat), float).reshape(x.shape))


def category_values(raw):
    raw = np.asarray(raw, dtype=float)
    out = np.zeros(raw.shape[:-1] + (len(CATS),))
    for i, (made, att) in enumerate((("fgm", "fga"), ("ftm", "fta"))):
        np.divide(raw[..., IX[made]], raw[..., IX[att]], out=out[..., i], where=raw[..., IX[att]] > 0)
    for i, key in enumerate(CATS[2:], 2):
        out[..., i] = raw[..., IX[key]]
    return out


def impact_features(raw, reference):
    """Linear counting/extra-makes features on a FIXED reference population."""
    raw = np.asarray(raw, dtype=float)
    out = category_values(raw)
    ref = np.asarray(reference).sum(axis=0)
    for i, (m, a) in enumerate((("fgm", "fga"), ("ftm", "fta"))):
        pct = ref[IX[m]] / ref[IX[a]] if ref[IX[a]] else 0.
        out[..., i] = raw[..., IX[m]] - pct * raw[..., IX[a]]
    return out * SIGN


def category_moments(mean, cov):
    """Delta-method variance of team ratios, including makes/attempt covariance."""
    mean, cov = np.asarray(mean), np.asarray(cov)
    value = category_values(mean)
    var = np.zeros_like(value)
    for i, (m, a) in enumerate((("fgm", "fga"), ("ftm", "fta"))):
        mi, ai = IX[m], IX[a]
        den = np.maximum(mean[..., ai], 1e-9)
        gm, ga = 1 / den, -mean[..., mi] / den**2
        var[..., i] = gm**2 * cov[..., mi, mi] + ga**2 * cov[..., ai, ai] + 2 * gm * ga * cov[..., mi, ai]
    for i, key in enumerate(CATS[2:], 2):
        var[..., i] = cov[..., IX[key], IX[key]]
    return value, np.maximum(var, 0)


def independent_match_value(probabilities):
    """P(more category wins than losses) + .5 P(equal), no category ties.

    Poisson-binomial DP; this is exact only conditional on independence.
    For actual tied categories use sampled outcomes / the provider's rules.
    """
    p = np.asarray(probabilities)
    dp = np.zeros(p.shape[:-1] + (p.shape[-1] + 1,))
    dp[..., 0] = 1.
    for i in range(p.shape[-1]):
        new = dp * (1 - p[..., i, None])
        new[..., 1:] += dp[..., :-1] * p[..., i, None]
        dp = new
    n = p.shape[-1]
    return dp[..., n // 2 + 1:].sum(axis=-1) + (0.5 * dp[..., n // 2] if n % 2 == 0 else 0.)


def matchup_probabilities(my_mean, my_cov, opp_mean, opp_cov):
    mine, mv = category_moments(my_mean, my_cov)
    opp, ov = category_moments(opp_mean, opp_cov)
    diff = (mine - opp) * SIGN
    sd = np.sqrt(mv + ov)
    p = cdf(diff / np.maximum(sd, 1e-9))
    return np.where(sd > 1e-9, p, np.where(diff > 0, 1., np.where(diff < 0, 0., .5)))


def exact_week_results(my_raw, opp_raw):
    """Actual outcome evaluation; raw totals in, category ties handled explicitly."""
    diff = (category_values(my_raw) - category_values(opp_raw)) * SIGN
    tied = np.isclose(diff, 0, atol=1e-10, rtol=0)
    wins = ((diff > 0) & ~tied).sum(axis=-1)
    losses = ((diff < 0) & ~tied).sum(axis=-1)
    return wins + .5 * tied.sum(axis=-1), (wins > losses) + .5 * (wins == losses)


def best_lineup(players, slots):
    """Small exact assignment DP. Each (value, eligible slots) player used once.

    This demonstrates marginal startable points on one day, not a full
    season optimizer. Unfilled slots and negative-value players can be skipped.
    """
    dp = {0: 0.}
    for value, eligible in players:
        nxt = dict(dp)
        for mask, total in dp.items():
            for i, slot in enumerate(slots):
                if not mask & (1 << i) and (slot in eligible or slot == "UT"):
                    key = mask | (1 << i)
                    nxt[key] = max(nxt.get(key, -np.inf), total + value)
        dp = nxt
    return max(dp.values())


@dataclass(frozen=True)
class MinuteEvent:
    """A manually normalized evidence fixture, NOT an automated news parser.

    delta is an absolute scenario vs baseline, never an increment to the last
    event-adjusted result. A revision replaces earlier evidence in its cluster.
    """
    event_id: str
    cluster: str
    published_at: datetime
    observed_at: datetime
    effective_at: datetime
    valid_until: datetime
    delta: float
    probability: float
    half_life_hours: float | None = None

    def __post_init__(self):
        if any(t.tzinfo is None for t in (self.published_at, self.observed_at, self.effective_at, self.valid_until)):
            raise ValueError("event timestamps must be timezone-aware")
        if not 0 <= self.probability <= 1 or not np.isfinite(self.delta):
            raise ValueError("invalid event effect")
        if self.valid_until <= self.effective_at:
            raise ValueError("empty event validity interval")
        if self.half_life_hours is not None and self.half_life_hours <= 0:
            raise ValueError("half life must be positive")


def adjust_minutes(baseline, events, as_of, *, absorbed_clusters=frozenset()):
    """Deduplicate/revise then apply active evidence. Explicit absorption prevents
    adding the same injury/role information already present in a newer baseline.
    Structural events have no decay; rumors may have a configured half life.
    Multiple independent clusters add here, capped at 0..48; team minutes
    conservation and interaction handling are deliberately left for production.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    latest = {}
    for e in events:
        if e.published_at > as_of or e.observed_at > as_of or e.effective_at > as_of:
            continue
        if e.cluster in absorbed_clusters:
            continue
        key = (e.published_at, e.observed_at, e.event_id)
        prev = latest.get(e.cluster)
        if prev is None or key > (prev.published_at, prev.observed_at, prev.event_id):
            latest[e.cluster] = e
    delta = 0.
    for e in latest.values():
        # Expired revisions do not resurrect an older report.
        if as_of >= e.valid_until:
            continue
        age = max(0., (as_of - e.published_at).total_seconds() / 3600)
        decay = 2 ** (-age / e.half_life_hours) if e.half_life_hours else 1.
        delta += e.probability * e.delta * decay
    return float(np.clip(baseline + delta, 0, 48))
