"""Data shapes shared by the scoring engine: stat lines, league settings, category results."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Iterable, Mapping

from services.scoring.vocab import STATS, label_for


@dataclass
class StatLine:
    """Canonical raw stat totals for one player-game, one window, or one team.

    Rate stats (fg_pct, ft_pct, ...) are never stored; `get()` derives them from
    makes/attempts so they stay correct when lines are summed.
    """

    pts: float = 0.0
    reb: float = 0.0
    ast: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    tov: float = 0.0
    fgm: float = 0.0
    fga: float = 0.0
    fg3m: float = 0.0
    fg3a: float = 0.0
    ftm: float = 0.0
    fta: float = 0.0
    min: float = 0.0
    oreb: float = 0.0
    dreb: float = 0.0
    pf: float = 0.0
    dd: float = 0.0
    td: float = 0.0
    gp: float = 0.0

    # Columns shared by nba.player_game_stats / live_player_stats / player_rolling_stats / player_season_stats
    ROW_KEYS: ClassVar[tuple[str, ...]] = (
        "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "min",
    )

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @classmethod
    def from_row(cls, row: Any, gp: float = 1.0) -> "StatLine":
        return cls(**{k: float(getattr(row, k, 0) or 0) for k in cls.ROW_KEYS}, gp=gp)

    @classmethod
    def from_game_row(cls, row: Any) -> "StatLine":
        """Game-grain row: also derives double-double / triple-double flags."""
        line = cls.from_row(row, gp=1.0)
        tens = sum(1 for v in (line.pts, line.reb, line.ast, line.stl, line.blk) if v >= 10)
        line.dd = 1.0 if tens >= 2 else 0.0
        line.td = 1.0 if tens >= 3 else 0.0
        return line

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StatLine":
        names = set(cls.field_names())
        return cls(**{k: float(v or 0) for k, v in d.items() if k in names})

    def get(self, key: str) -> float:
        d = STATS.get(key)
        if d is not None and d.is_rate:
            num = getattr(self, d.numerator, 0.0)
            den = getattr(self, d.denominator, 0.0)
            return num / den if den else 0.0
        return float(getattr(self, key, 0.0))

    def scaled(self, factor: float) -> "StatLine":
        return StatLine(**{k: getattr(self, k) * factor for k in self.field_names()})

    def __add__(self, other: "StatLine") -> "StatLine":
        return StatLine(**{k: getattr(self, k) + getattr(other, k) for k in self.field_names()})

    @staticmethod
    def sum(lines: Iterable["StatLine"]) -> "StatLine":
        total = StatLine()
        for line in lines:
            total = total + line
        return total

    def to_dict(self, keys: Iterable[str]) -> dict[str, float]:
        return {k: self.get(k) for k in keys}


@dataclass(frozen=True)
class CategoryDef:
    key: str
    label: str
    higher_is_better: bool = True
    is_rate: bool = False

    def to_json(self) -> dict:
        return {"key": self.key, "label": self.label,
                "higher_is_better": self.higher_is_better, "is_rate": self.is_rate}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "CategoryDef":
        return cls(key=d["key"], label=d.get("label") or label_for(d["key"]),
                   higher_is_better=bool(d.get("higher_is_better", True)), is_rate=bool(d.get("is_rate", False)))

    @classmethod
    def for_key(cls, key: str, higher_is_better: bool | None = None) -> "CategoryDef":
        d = STATS[key]
        return cls(key=key, label=d.label,
                   higher_is_better=d.higher_is_better if higher_is_better is None else higher_is_better,
                   is_rate=d.is_rate)


@dataclass
class LeagueSettings:
    """Provider-agnostic league settings as parsed from ESPN / Yahoo."""

    provider: str                           # espn | yahoo
    provider_league_id: str
    season: int
    name: str | None
    scoring_type: str                       # points | categories | roto
    category_win_mode: str | None           # each_category | most_categories
    categories: list[CategoryDef] = field(default_factory=list)
    point_weights: dict[str, float] = field(default_factory=dict)
    matchup_periods: dict = field(default_factory=dict)
    roster_slots: dict[str, int] = field(default_factory=dict)
    position_limits: dict[str, int] = field(default_factory=dict)   # hard caps, e.g. {"C": 4}; 0 = none allowed
    draft_settings: dict = field(default_factory=dict)
    raw_settings: dict = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CategoryTeamScoreData:
    totals: dict[str, float]                # category key -> value (rates as 0-1 fractions)
    raw: dict[str, float] | None = None     # fgm/fga/ftm/fta/fg3m/fg3a when known
    wins: int = 0
    losses: int = 0
    ties: int = 0
    live_adjusted: bool = False


@dataclass
class CategoryItemResult:
    key: str
    label: str
    you: float
    opp: float
    winner: str                             # you | opp | tie
    higher_is_better: bool
    is_rate: bool


@dataclass
class CategoryComparisonData:
    items: list[CategoryItemResult]
    wins: int
    losses: int
    ties: int
