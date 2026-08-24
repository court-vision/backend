"""Head-to-head category scoring: per-category team totals, comparisons, overlays, projections."""

from typing import Iterable

from services.scoring.models import (
    CategoryComparisonData,
    CategoryDef,
    CategoryItemResult,
    CategoryTeamScoreData,
    StatLine,
)

RAW_KEYS: tuple[str, ...] = ("fgm", "fga", "ftm", "fta", "fg3m", "fg3a")


class CategoryScoring:
    format = "categories"
    RATE_EPS = 1e-6
    RATE_DECIMALS = 4

    def __init__(self, categories: list[CategoryDef], win_mode: str = "each_category"):
        self.categories = list(categories)
        self.win_mode = win_mode or "each_category"

    @property
    def keys(self) -> list[str]:
        return [c.key for c in self.categories]

    # ---- totals -------------------------------------------------------------

    def totals_from_line(self, total: StatLine) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.categories:
            value = total.get(c.key)
            out[c.key] = round(value, self.RATE_DECIMALS) if c.is_rate else value
        return out

    def team_totals(self, lines: Iterable[StatLine]) -> dict[str, float]:
        return self.totals_from_line(StatLine.sum(lines))

    @staticmethod
    def raw_from_line(total: StatLine) -> dict[str, float]:
        return {k: total.get(k) for k in RAW_KEYS}

    # ---- comparison ---------------------------------------------------------

    def compare(self, you: dict[str, float], opp: dict[str, float]) -> CategoryComparisonData:
        items: list[CategoryItemResult] = []
        wins = losses = ties = 0
        for c in self.categories:
            y = you.get(c.key)
            o = opp.get(c.key)
            if y is None or o is None:
                winner = "tie"
            else:
                diff = float(y) - float(o)
                if (c.is_rate and abs(diff) < self.RATE_EPS) or diff == 0:
                    winner = "tie"
                else:
                    winner = "you" if (diff > 0) == c.higher_is_better else "opp"
            if winner == "you":
                wins += 1
            elif winner == "opp":
                losses += 1
            else:
                ties += 1
            items.append(CategoryItemResult(
                key=c.key, label=c.label, you=float(y or 0), opp=float(o or 0),
                winner=winner, higher_is_better=c.higher_is_better, is_rate=c.is_rate,
            ))
        return CategoryComparisonData(items=items, wins=wins, losses=losses, ties=ties)

    @staticmethod
    def week_won(cmp: CategoryComparisonData) -> bool | None:
        """True/False for a decided week, None for a tie. Both win modes reduce to wins vs losses."""
        if cmp.wins > cmp.losses:
            return True
        if cmp.wins < cmp.losses:
            return False
        return None

    # ---- overlay / projection ----------------------------------------------

    def overlay(self, base: CategoryTeamScoreData, delta: StatLine) -> CategoryTeamScoreData:
        """Add raw stats (e.g. tonight's live box scores) on top of a provider snapshot.

        Counting categories add directly. Rate categories are recomputed from
        makes/attempts only when the base carries them; otherwise they are left
        untouched rather than approximated.
        """
        totals = dict(base.totals)
        raw = dict(base.raw) if base.raw is not None else None
        if raw is not None:
            for k in RAW_KEYS:
                raw[k] = raw.get(k, 0.0) + delta.get(k)
        for c in self.categories:
            if not c.is_rate:
                totals[c.key] = totals.get(c.key, 0.0) + delta.get(c.key)
            elif raw is not None:
                d = _rate_parts(c.key)
                if d:
                    num, den = raw.get(d[0], 0.0), raw.get(d[1], 0.0)
                    totals[c.key] = round(num / den, self.RATE_DECIMALS) if den else 0.0
        return CategoryTeamScoreData(totals=totals, raw=raw, wins=base.wins, losses=base.losses,
                                     ties=base.ties, live_adjusted=True)

    def project(self, base: CategoryTeamScoreData,
                roster: Iterable[tuple[StatLine, int, bool]]) -> dict[str, float]:
        """Project end-of-week totals: base + Σ(avg line × games remaining) for counted players."""
        future = StatLine.sum(avg.scaled(games) for avg, games, counts in roster if counts and games > 0)
        return self.overlay(base, future).totals


def _rate_parts(key: str) -> tuple[str, str] | None:
    from services.scoring.vocab import STATS
    d = STATS.get(key)
    if d is None or not d.is_rate:
        return None
    return d.numerator, d.denominator
