"""
League valuation: one projected pool, valued the way *this* league scores.

The board used to rank categories by per-game z-scores over the whole pool. Two
things were missing from that, and both cost drafts:

- **Volume.** A per-game z never sees games played, so a player projected for
  50 games and one projected for 75 with the same line ranked together. In a
  head-to-head week, and over a roto season, the 75-game player contributes
  half again as much.
- **Noise.** Every category counted the same, but a week of steals is mostly
  luck while a week of rebounds is mostly the player. Rosenof's G-score
  (arXiv 2307.02188) adds each category's week-to-week noise to the
  denominator, so a steals lead is priced like the coin flip it is. In the
  2026-09-05 redraft experiments (`experiments/ranking_engine/`) that alone
  took the match score from 41.5% to 69.7%.

**The unit is the week.** A player's expected weekly total in category c is
`x_c × g`, his per-game line times his effective games per week. Against the
*draftable cohort* — the top `league_size × roster_size` players, found by
iterating the ranking three times, because the players nobody drafts should not
set the average everybody is measured against —

    G_c = sign_c × (x_c·g − mean_cohort) / sqrt(var_cohort + tau2_c)

where `tau2_c = PHI[c] × mean_cohort` is the cohort's average week-to-week
variance. `PHI` is per-game variance over mean, measured on 2025-26 game logs
(26,422 player-games, the 343 players with 20+ games at 15+ minutes): steals,
blocks and turnovers sit near 1.0 (Poisson), points at 3.2. Shooting categories
are valued as impact — makes beyond a cohort-average shooter on the same
attempts — and their noise is binomial, `PHI × attempts` (0.23 FG, 0.18 FT;
p(1−p) predicts both). A punted category is simply not in the sum.

**Roto** has no weeks: season totals against the cohort, no noise term.
**Points** is the league's own weights times effective games — double- and
triple-double weights included when the projection carries their per-game
rates.

**Effective games** are where the fantasy playoffs come in. A player's expected
games are spread over his team's schedule: those in the regular fantasy weeks
count once, those in the playoff weeks count `playoff_weight` times (the
drafter's λ, 2 by default), and those in any week after the playoffs count not
at all — no head-to-head matchup is played then. Everything is renormalized so
the league-average player keeps his games: λ changes who ranks where, not how
big the numbers are. Roto counts the whole schedule, once.

The output keeps the board's existing contract — a pool row, a display value,
per-category display values, per-category z, and their sum, best first — and
adds what the engine alone knows: the season value that orders it and the
effective games behind it.

Everything here is pure: no database, no calendar reads, no I/O.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from services.scoring.category_rank import PoolRow
from services.scoring.category_value import CATEGORY_VALUE_OFFSET, CATEGORY_VALUE_SCALE
from services.scoring.models import CategoryDef, StatLine
from services.scoring.vocab import STATS

# Per-game variance / mean, 2025-26 game logs (see module docstring). Counting
# categories not measured are treated as Poisson.
PHI: dict[str, float] = {
    "pts": 3.20, "reb": 1.40, "ast": 1.28, "stl": 1.06, "blk": 1.09,
    "tov": 1.03, "fg3m": 1.18,
    # Shooting impact: variance per attempt of (makes − p̄ × attempts).
    "fg_pct": 0.227, "ft_pct": 0.176, "fg3_pct": 0.230,
}
DEFAULT_PHI_COUNTING = 1.0
DEFAULT_PHI_RATE = 0.23

COHORT_ITERATIONS = 3
DEFAULT_LEAGUE_SIZE = 12
DEFAULT_ROSTER_SIZE = 13
DEFAULT_GAMES = 65.0            # a healthy rotation season, when nothing projects one
DEFAULT_COUNTED_WEEKS = 23.0    # ESPN's default head-to-head season, weeks 1-23
NBA_SEASON_GAMES = 82.0
DEFAULT_PLAYOFF_WEIGHT = 2.0
PLAYOFF_WEIGHTS: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0, 4.0)

VALUE_DECIMALS = 1
Z_DECIMALS = 3
RATE_DECIMALS = 4
CAT_VALUE_DECIMALS = 2


@dataclass(frozen=True)
class TeamWeeks:
    """How one NBA team's schedule falls across a league's fantasy calendar."""

    regular: int                # games in regular-season fantasy weeks
    playoff: int = 0            # games in the league's playoff weeks
    after: int = 0              # games after the fantasy season ends (never scored)

    @property
    def total(self) -> int:
        return self.regular + self.playoff + self.after


@dataclass(frozen=True)
class LeagueModel:
    """Everything about a league the valuation reads.

    `format` is `points`, `categories` (head-to-head) or `roto`.
    `team_weeks` maps a team code to its schedule split; a team missing from it
    (or an empty map) is treated as the league average, so a calendar that
    cannot be read degrades to plain expected games rather than to nothing.
    """

    format: str
    categories: tuple[CategoryDef, ...] = ()
    point_weights: Mapping[str, float] = field(default_factory=dict)
    league_size: int = DEFAULT_LEAGUE_SIZE
    roster_size: int = DEFAULT_ROSTER_SIZE
    counted_weeks: float = 0.0              # length of the scored season, in 7-day weeks
    team_weeks: Mapping[str, TeamWeeks] = field(default_factory=dict)
    playoff_weight: float = DEFAULT_PLAYOFF_WEIGHT

    @property
    def cohort_size(self) -> int:
        return max(1, int(self.league_size) * int(self.roster_size))

    @property
    def weighs_playoffs(self) -> bool:
        return self.format != "roto" and any(t.playoff for t in self.team_weeks.values())


@dataclass(frozen=True)
class ProjectedPlayer:
    """One player as the valuation sees him: a per-game line, games, a team."""

    row: PoolRow
    games: Optional[float] = None           # expected games this season (None -> DEFAULT_GAMES)
    team: Optional[str] = None              # current NBA team code, for the schedule split
    dd_rate: Optional[float] = None         # P(double-double) per game, points leagues
    td_rate: Optional[float] = None


@dataclass(frozen=True)
class Valued:
    """One valued player. The first five fields are the board's historical tuple."""

    row: PoolRow
    value: float                                    # display: fpts per game, or the category index
    cats: Optional[dict[str, Optional[float]]]      # per-category display values (per game)
    z: Optional[dict[str, float]]                   # per-category contribution, signed
    z_sum: Optional[float]
    season_value: float                             # what the board is ordered by
    games: float                                    # effective games behind it
    expected_games: float                           # games the projection expects, unweighted
    share: float = 1.0                              # the fraction of his team's games those are


# ---- effective games ---------------------------------------------------------------


def _norm(model: LeagueModel) -> float:
    """Scale that keeps the league-average team's games unchanged under λ."""
    teams = list(model.team_weeks.values())
    if not teams:
        return 1.0
    counted = statistics.fmean(t.regular + t.playoff for t in teams)
    weighted = statistics.fmean(t.regular + model.playoff_weight * t.playoff for t in teams)
    return counted / weighted if weighted > 0 else 1.0


def _average_split(model: LeagueModel) -> Optional[TeamWeeks]:
    teams = list(model.team_weeks.values())
    if not teams:
        return None
    return TeamWeeks(
        regular=round(statistics.fmean(t.regular for t in teams)),
        playoff=round(statistics.fmean(t.playoff for t in teams)),
        after=round(statistics.fmean(t.after for t in teams)),
    )


def _split_of(player: ProjectedPlayer, model: LeagueModel) -> Optional[TeamWeeks]:
    """His team's schedule split; the league-average one for a team not on the calendar."""
    split = model.team_weeks.get(player.team) if player.team else None
    return split or _average_split(model)


def games_share(player: ProjectedPlayer, model: LeagueModel) -> float:
    """The fraction of his team's games he is expected to play, at most 1.

    Against an 82-game season when no calendar says how many his team plays.
    """
    games = DEFAULT_GAMES if player.games is None else max(float(player.games), 0.0)
    split = _split_of(player, model)
    total = split.total if (split is not None and split.total > 0) else NBA_SEASON_GAMES
    return min(games / total, 1.0)


def effective_games(player: ProjectedPlayer, model: LeagueModel, norm: Optional[float] = None) -> float:
    """Expected games, weighted by when they are played (see module docstring)."""
    games = DEFAULT_GAMES if player.games is None else max(float(player.games), 0.0)
    split = _split_of(player, model)
    if split is None or split.total <= 0:
        return games
    share = games_share(player, model)
    if model.format == "roto":
        return share * split.total
    weighted = split.regular + model.playoff_weight * split.playoff
    return share * weighted * (norm if norm is not None else _norm(model))


# ---- points ----------------------------------------------------------------------


def points_per_game(player: ProjectedPlayer, weights: Mapping[str, float]) -> float:
    line = player.row.line
    total = 0.0
    for key, w in weights.items():
        if key == "dd":
            total += w * (player.dd_rate or 0.0)
        elif key == "td":
            total += w * (player.td_rate or 0.0)
        else:
            total += w * line.get(key)
    return total


def _value_points(players: Sequence[ProjectedPlayer], model: LeagueModel) -> list[Valued]:
    norm = _norm(model)
    out: list[Valued] = []
    for p in players:
        eg = effective_games(p, model, norm)
        fpg = points_per_game(p, model.point_weights)
        out.append(Valued(
            row=p.row, value=round(fpg, VALUE_DECIMALS), cats=None, z=None, z_sum=None,
            season_value=round(fpg * eg, VALUE_DECIMALS), games=round(eg, 1),
            expected_games=DEFAULT_GAMES if p.games is None else float(p.games),
            share=games_share(p, model),
        ))
    out.sort(key=lambda v: (-v.season_value, -v.value, v.row.id))
    return out


# ---- categories -------------------------------------------------------------------


def _phi(cat: CategoryDef) -> float:
    d = STATS.get(cat.key)
    default = DEFAULT_PHI_RATE if (d is not None and d.is_rate) else DEFAULT_PHI_COUNTING
    return PHI.get(cat.key, default)


def _display(line: StatLine, cat: CategoryDef) -> Optional[float]:
    d = STATS[cat.key]
    if d.is_rate:
        attempts = line.get(d.denominator)
        return round(line.get(cat.key), RATE_DECIMALS) if attempts > 0 else None
    return round(line.get(cat.key), CAT_VALUE_DECIMALS)


def _clean(x: float, decimals: int) -> float:
    return round(x, decimals) or 0.0


def _category_scores(
    players: Sequence[ProjectedPlayer],
    model: LeagueModel,
    per_period: Sequence[float],
    cohort: Sequence[int],
    with_noise: bool,
) -> tuple[list[dict[str, float]], list[float]]:
    """Per-category signed scores for every player against `cohort` (indices).

    `per_period` is each player's games in the unit being compared: games per
    week for head-to-head, the season's games for roto.
    """
    zs: list[dict[str, float]] = [{} for _ in players]
    sums = [0.0] * len(players)
    for cat in model.categories:
        d = STATS[cat.key]
        sign = 1.0 if cat.higher_is_better else -1.0
        if d.is_rate:
            makes = [p.row.line.get(d.numerator) * n for p, n in zip(players, per_period)]
            attempts = [p.row.line.get(d.denominator) * n for p, n in zip(players, per_period)]
            c_makes = sum(makes[i] for i in cohort)
            c_att = sum(attempts[i] for i in cohort)
            pbar = c_makes / c_att if c_att > 0 else 0.0
            totals = [m - pbar * a for m, a in zip(makes, attempts)]
            noise = [_phi(cat) * a for a in attempts]
        else:
            totals = [p.row.line.get(cat.key) * n for p, n in zip(players, per_period)]
            noise = [_phi(cat) * max(t, 0.0) for t in totals]
        c_vals = [totals[i] for i in cohort]
        mean = statistics.fmean(c_vals) if c_vals else 0.0
        var = statistics.pvariance(c_vals, mean) if len(c_vals) > 1 else 0.0
        tau2 = statistics.fmean(noise[i] for i in cohort) if (with_noise and cohort) else 0.0
        denom = (var + tau2) ** 0.5
        for i, t in enumerate(totals):
            z = sign * (t - mean) / denom if denom > 1e-12 else 0.0
            zs[i][cat.key] = z
            sums[i] += z
    return zs, sums


def _value_categories(players: Sequence[ProjectedPlayer], model: LeagueModel) -> list[Valued]:
    if not players:
        return []
    roto = model.format == "roto"
    norm = _norm(model)
    games = [effective_games(p, model, norm) for p in players]
    # Head-to-head compares weekly totals. The noise term is per week, so the
    # unit matters: with no calendar, assume ESPN's default season length.
    weeks = model.counted_weeks if model.counted_weeks > 0 else DEFAULT_COUNTED_WEEKS
    per_period = games if roto else [g / weeks for g in games]

    # Seed the cohort on per-game volume-free value (ESPN default points), then
    # let the category score choose its own cohort.
    from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

    seed = [points_per_game(p, DEFAULT_POINT_WEIGHTS) * g for p, g in zip(players, games)]
    n = min(model.cohort_size, len(players))
    cohort = sorted(range(len(players)), key=lambda i: -seed[i])[:n]
    zs: list[dict[str, float]] = []
    sums: list[float] = []
    for _ in range(COHORT_ITERATIONS):
        zs, sums = _category_scores(players, model, per_period, cohort, with_noise=not roto)
        cohort = sorted(range(len(players)), key=lambda i: -sums[i])[:n]

    out: list[Valued] = []
    for i, p in enumerate(players):
        z = {k: _clean(v, Z_DECIMALS) for k, v in zs[i].items()}
        z_sum = _clean(sums[i], Z_DECIMALS)
        index = max(0.0, round(CATEGORY_VALUE_OFFSET + CATEGORY_VALUE_SCALE * z_sum, VALUE_DECIMALS))
        out.append(Valued(
            row=p.row,
            value=index,
            cats={c.key: _display(p.row.line, c) for c in model.categories},
            z=z,
            z_sum=z_sum,
            # The z-sum already carries volume, so the season number is the
            # index at a fixed season length — the same scale `value x gp`
            # always produced for an average-length season.
            season_value=round(index * DEFAULT_GAMES, VALUE_DECIMALS),
            games=round(games[i], 1),
            expected_games=DEFAULT_GAMES if p.games is None else float(p.games),
            share=games_share(p, model),
        ))
    out.sort(key=lambda v: (-(v.z_sum or 0.0), -v.row.fpts_avg, v.row.id))
    return out


def value_pool(players: Sequence[ProjectedPlayer], model: LeagueModel) -> list[Valued]:
    """The pool, valued for `model` and best first."""
    if model.format in ("categories", "roto") and model.categories:
        return _value_categories(players, model)
    return _value_points(players, model)


def playoff_weight_of(raw: object) -> float:
    """A requested λ, snapped to the offered steps; anything unreadable is the default."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PLAYOFF_WEIGHT
    return min(PLAYOFF_WEIGHTS, key=lambda w: (abs(w - value), w))
