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

**A missed game** is not an empty seat. With `fill_missed` (set when the
player's games come from the live projection, which says which of his team's
games he is expected to play), the games he misses are played by a replacement:
the level of the first player past the draftable cohort, ranked per game among
those expected to play at all. His season is then

    his line × the games he plays + min(his line, the replacement's) × the games he misses

so a star who is out for six weeks costs the gap to a replacement for six
weeks, not six weeks of nothing, and a player below the replacement gains
nothing by sitting. In a category league the same rule is applied to the
weekly totals, the replacement's line being the average of the best
`league_size` players past the cohort. A player with no game left that the
league scores (out until after its playoffs, in head-to-head) gets no
replacement: nobody holds his seat for him, and he falls off the board.
Without the flag nothing here applies and every valuation is what it was.

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
# Under `fill_missed`: fewer expected games than this in the scored season
# and he is not coming back, so no replacement plays for him and he is no
# replacement himself.
FILL_MIN_GAMES = 0.5

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
    # A game he misses is played by a replacement (see module docstring). Only
    # for games that say which he misses: the live projection's.
    fill_missed: bool = False

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
    # Where on the calendar `games` fall, when the projection says: his expected
    # games in the regular fantasy weeks, the playoff weeks, and after them.
    # None spreads `games` over his team's schedule evenly.
    phase_games: Optional[tuple[float, float, float]] = None


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
    scale = norm if norm is not None else _norm(model)
    if player.phase_games is not None:
        # The projection placed his games itself: no even spread to assume.
        regular, playoff, after = (max(float(g), 0.0) for g in player.phase_games)
        if model.format == "roto":
            return regular + playoff + after
        return (regular + model.playoff_weight * playoff) * scale
    share = games_share(player, model)
    if model.format == "roto":
        return share * split.total
    weighted = split.regular + model.playoff_weight * split.playoff
    return share * weighted * scale


def full_games(player: ProjectedPlayer, model: LeagueModel, norm: Optional[float] = None) -> Optional[float]:
    """The effective games of a player who misses none of his team's: what a
    seat is worth filling. None when no calendar says how many those are."""
    split = _split_of(player, model)
    if split is None or split.total <= 0:
        return None
    if model.format == "roto":
        return float(split.total)
    return (split.regular + model.playoff_weight * split.playoff) * (norm if norm is not None else _norm(model))


def scored_games(player: ProjectedPlayer, model: LeagueModel) -> float:
    """His expected games that the league scores: every game left in roto, the
    games before the fantasy season ends otherwise (the weeks after it count
    for nobody). Unweighted, unlike `effective_games`."""
    games = DEFAULT_GAMES if player.games is None else max(float(player.games), 0.0)
    if player.phase_games is not None:
        regular, playoff, after = (max(float(g), 0.0) for g in player.phase_games)
        return regular + playoff + (after if model.format == "roto" else 0.0)
    split = _split_of(player, model)
    if model.format == "roto" or split is None or split.total <= 0:
        return games
    return games_share(player, model) * (split.regular + split.playoff)


def _plays(player: ProjectedPlayer, model: Optional[LeagueModel] = None) -> bool:
    """Whether he is expected to play again in a game that counts (`FILL_MIN_GAMES`):
    a player back only after the fantasy playoffs holds no seat and is nobody's
    replacement. With no model, any game left."""
    if player.games is None:
        return True
    games = scored_games(player, model) if model is not None else float(player.games)
    return games >= FILL_MIN_GAMES


def _missed(players: Sequence[ProjectedPlayer], games: Sequence[float], model: LeagueModel, norm: float) -> list[float]:
    """Under `fill_missed`, the effective games each player's seat is filled by
    a replacement; zeros without it, and for a player who is not coming back."""
    if not model.fill_missed:
        return [0.0] * len(players)
    out: list[float] = []
    for p, eg in zip(players, games):
        full = full_games(p, model, norm) if _plays(p, model) else None
        out.append(max(full - eg, 0.0) if full is not None else 0.0)
    return out


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


def replacement_level(
    per_game: Sequence[float], players: Sequence[ProjectedPlayer], size: int,
    model: Optional[LeagueModel] = None,
) -> float:
    """The per-game value of the first player past a cohort of `size`, ranked per
    game among those expected to play in a game `model` scores; 0 for a pool no
    deeper than the cohort."""
    ranked = sorted((v for v, p in zip(per_game, players) if _plays(p, model)), reverse=True)
    return ranked[size] if len(ranked) > size else 0.0


def _value_points(players: Sequence[ProjectedPlayer], model: LeagueModel) -> list[Valued]:
    norm = _norm(model)
    games = [effective_games(p, model, norm) for p in players]
    per_game = [points_per_game(p, model.point_weights) for p in players]
    missed = _missed(players, games, model, norm)
    level = replacement_level(per_game, players, model.cohort_size, model) if model.fill_missed else 0.0
    out: list[Valued] = []
    for p, eg, fpg, gone in zip(players, games, per_game, missed):
        out.append(Valued(
            row=p.row, value=round(fpg, VALUE_DECIMALS), cats=None, z=None, z_sum=None,
            season_value=round(fpg * eg + min(fpg, level) * gone, VALUE_DECIMALS), games=round(eg, 1),
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


# One seat's share of a period that someone else plays: the games, and whose line.
Fill = Optional[tuple[float, StatLine]]


def _category_scores(
    players: Sequence[ProjectedPlayer],
    model: LeagueModel,
    per_period: Sequence[float],
    cohort: Sequence[int],
    with_noise: bool,
    fill: Optional[Sequence[Fill]] = None,
) -> tuple[list[dict[str, float]], list[float], dict[str, tuple[float, float, Optional[float]]]]:
    """Per-category signed scores for every player against `cohort` (indices).

    `per_period` is each player's games in the unit being compared: games per
    week for head-to-head, the season's games for roto. `fill`, when given, is
    what plays the rest of each seat's period (`fill_missed`): the totals are
    then the seat's, his games and the fill's together.

    Also returns each category's scale, `(sign, denominator, cohort rate)`: what
    one game of any line is worth on this board (`_worth`).
    """
    def totals_of(key: str) -> list[float]:
        own = [p.row.line.get(key) * n for p, n in zip(players, per_period)]
        if fill is None:
            return own
        return [t + (f[1].get(key) * f[0] if f is not None else 0.0) for t, f in zip(own, fill)]

    zs: list[dict[str, float]] = [{} for _ in players]
    sums = [0.0] * len(players)
    scale: dict[str, tuple[float, float, Optional[float]]] = {}
    for cat in model.categories:
        d = STATS[cat.key]
        sign = 1.0 if cat.higher_is_better else -1.0
        pbar: Optional[float] = None
        if d.is_rate:
            makes = totals_of(d.numerator)
            attempts = totals_of(d.denominator)
            c_makes = sum(makes[i] for i in cohort)
            c_att = sum(attempts[i] for i in cohort)
            pbar = c_makes / c_att if c_att > 0 else 0.0
            totals = [m - pbar * a for m, a in zip(makes, attempts)]
            noise = [_phi(cat) * a for a in attempts]
        else:
            totals = totals_of(cat.key)
            noise = [_phi(cat) * max(t, 0.0) for t in totals]
        c_vals = [totals[i] for i in cohort]
        mean = statistics.fmean(c_vals) if c_vals else 0.0
        var = statistics.pvariance(c_vals, mean) if len(c_vals) > 1 else 0.0
        tau2 = statistics.fmean(noise[i] for i in cohort) if (with_noise and cohort) else 0.0
        denom = (var + tau2) ** 0.5
        scale[cat.key] = (sign, denom, pbar)
        for i, t in enumerate(totals):
            z = sign * (t - mean) / denom if denom > 1e-12 else 0.0
            zs[i][cat.key] = z
            sums[i] += z
    return zs, sums, scale


def _worth(line: StatLine, scale: Mapping[str, tuple[float, float, Optional[float]]]) -> float:
    """What one game of `line` adds to a seat's score, every category summed."""
    total = 0.0
    for key, (sign, denom, pbar) in scale.items():
        if denom <= 1e-12:
            continue
        d = STATS[key]
        amount = (line.get(d.numerator) - (pbar or 0.0) * line.get(d.denominator)) if d.is_rate else line.get(key)
        total += sign * amount / denom
    return total


def _mean_line(lines: Sequence[StatLine]) -> StatLine:
    out = lines[0].scaled(1.0 / len(lines))
    for line in lines[1:]:
        out = out + line.scaled(1.0 / len(lines))
    return out


def _fills(
    players: Sequence[ProjectedPlayer],
    model: LeagueModel,
    missed: Sequence[float],
    scale: Mapping[str, tuple[float, float, Optional[float]]],
) -> Optional[list[Fill]]:
    """Who plays the games each seat's player misses: the replacement, or the
    player's own line when he is no better than one (the points rule, `min`).

    The replacement's line is the average of the best `league_size` players
    past the cohort, ranked by what a game of theirs is worth here among those
    expected to play: one player's line would hand every injured guard a
    center's blocks. None when the pool is no deeper than the cohort.
    """
    worth = [_worth(p.row.line, scale) for p in players]
    ranked = sorted((i for i, p in enumerate(players) if _plays(p, model)), key=lambda i: -worth[i])
    band = ranked[model.cohort_size: model.cohort_size + max(int(model.league_size), 1)]
    if not band:
        return None
    replacement = _mean_line([players[i].row.line for i in band])
    level = _worth(replacement, scale)
    return [
        (gone, replacement if worth[i] > level else p.row.line) if gone > 0 else None
        for i, (p, gone) in enumerate(zip(players, missed))
    ]


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
    missed = _missed(players, games, model, norm)
    if not roto:
        missed = [m / weeks for m in missed]
    filling = model.fill_missed and any(m > 0 for m in missed)

    # Seed the cohort on per-game volume-free value (ESPN default points), then
    # let the category score choose its own cohort.
    from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

    seed = [points_per_game(p, DEFAULT_POINT_WEIGHTS) * g for p, g in zip(players, games)]
    n = min(model.cohort_size, len(players))
    cohort = sorted(range(len(players)), key=lambda i: -seed[i])[:n]
    zs: list[dict[str, float]] = []
    sums: list[float] = []
    fill: Optional[list[Fill]] = None
    # Filling the missed games needs a first pass to say what a game is worth,
    # so it gets one more: the last two are scored on filled seats.
    for _ in range(COHORT_ITERATIONS + (1 if filling else 0)):
        zs, sums, scale = _category_scores(players, model, per_period, cohort, with_noise=not roto, fill=fill)
        cohort = sorted(range(len(players)), key=lambda i: -sums[i])[:n]
        if filling:
            fill = _fills(players, model, missed, scale)

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
