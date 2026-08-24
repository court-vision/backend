"""
Canonical fantasy stat vocabulary.

Every stat has one canonical key here; provider ids (ESPN statId, Yahoo stat_id)
map onto it so scoring settings and box scores from either provider speak the
same language. Rate stats are defined by their numerator/denominator so they are
always recomputed from makes and attempts, never averaged.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StatDef:
    key: str
    label: str
    higher_is_better: bool = True
    is_rate: bool = False
    numerator: str | None = None
    denominator: str | None = None
    espn_id: int | None = None
    yahoo_id: int | None = None


STATS: dict[str, StatDef] = {
    "pts":     StatDef("pts", "PTS", espn_id=0, yahoo_id=15),
    "reb":     StatDef("reb", "REB", espn_id=6, yahoo_id=18),
    "oreb":    StatDef("oreb", "OREB", espn_id=4, yahoo_id=16),
    "dreb":    StatDef("dreb", "DREB", espn_id=5, yahoo_id=17),
    "ast":     StatDef("ast", "AST", espn_id=3, yahoo_id=19),
    "stl":     StatDef("stl", "STL", espn_id=2, yahoo_id=20),
    "blk":     StatDef("blk", "BLK", espn_id=1, yahoo_id=21),
    "tov":     StatDef("tov", "TO", higher_is_better=False, espn_id=11, yahoo_id=22),
    "fgm":     StatDef("fgm", "FGM", espn_id=13, yahoo_id=6),
    "fga":     StatDef("fga", "FGA", higher_is_better=False, espn_id=14, yahoo_id=5),
    "fg_pct":  StatDef("fg_pct", "FG%", is_rate=True, numerator="fgm", denominator="fga", espn_id=19, yahoo_id=7),
    "ftm":     StatDef("ftm", "FTM", espn_id=15, yahoo_id=9),
    "fta":     StatDef("fta", "FTA", higher_is_better=False, espn_id=16, yahoo_id=8),
    "ft_pct":  StatDef("ft_pct", "FT%", is_rate=True, numerator="ftm", denominator="fta", espn_id=20, yahoo_id=10),
    "fg3m":    StatDef("fg3m", "3PM", espn_id=17, yahoo_id=12),
    "fg3a":    StatDef("fg3a", "3PA", espn_id=18, yahoo_id=11),
    "fg3_pct": StatDef("fg3_pct", "3P%", is_rate=True, numerator="fg3m", denominator="fg3a", espn_id=21, yahoo_id=13),
    "pf":      StatDef("pf", "PF", higher_is_better=False, espn_id=9, yahoo_id=24),
    "min":     StatDef("min", "MIN", espn_id=40),
    "gp":      StatDef("gp", "GP", espn_id=42),
    "dd":      StatDef("dd", "DD", espn_id=37, yahoo_id=30),
    "td":      StatDef("td", "TD", espn_id=38, yahoo_id=31),
    "ato":     StatDef("ato", "A/TO", is_rate=True, numerator="ast", denominator="tov", espn_id=35, yahoo_id=23),
}

ESPN_ID_TO_KEY: dict[int, str] = {d.espn_id: d.key for d in STATS.values() if d.espn_id is not None}
YAHOO_ID_TO_KEY: dict[int, str] = {d.yahoo_id: d.key for d in STATS.values() if d.yahoo_id is not None}

# Yahoo "display only" composite stats: value like "245/510" (makes/attempts).
YAHOO_COMPOSITE_IDS: dict[int, tuple[str, str]] = {
    9004003: ("fgm", "fga"),
    9007006: ("ftm", "fta"),
}

COUNTING_KEYS: tuple[str, ...] = tuple(k for k, d in STATS.items() if not d.is_rate)
RATE_KEYS: tuple[str, ...] = tuple(k for k, d in STATS.items() if d.is_rate)

# The original hardcoded Court Vision formula (pipelines/transformers/fantasy_points.py),
# kept as the global default and the fallback when a league has no settings.
DEFAULT_POINT_WEIGHTS: dict[str, float] = {
    "pts": 1.0, "reb": 1.0, "ast": 2.0, "stl": 4.0, "blk": 4.0, "tov": -2.0,
    "fg3m": 1.0, "fgm": 2.0, "fga": -1.0, "ftm": 1.0, "fta": -1.0,
}

# Standard 9-cat head-to-head categories, in conventional display order.
DEFAULT_CATEGORIES: list[str] = ["fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"]


def label_for(key: str) -> str:
    d = STATS.get(key)
    return d.label if d else key.upper()
