"""Points-league scoring: a linear combination of counting stats."""

from typing import Any, Iterable, Mapping

from services.scoring.models import StatLine
from services.scoring.vocab import COUNTING_KEYS, DEFAULT_POINT_WEIGHTS


class PointsScoring:
    format = "points"

    def __init__(self, weights: Mapping[str, float]):
        # Rate stats cannot carry point weights; parsers reject them upstream.
        self.weights: dict[str, float] = {
            k: float(v) for k, v in weights.items() if k in COUNTING_KEYS and v
        }

    def score(self, line: StatLine) -> float:
        return sum(w * line.get(k) for k, w in self.weights.items())

    def score_row(self, row: Any) -> float:
        return self.score(StatLine.from_game_row(row))

    def team_total(self, lines: Iterable[StatLine]) -> float:
        return sum(self.score(line) for line in lines)

    @property
    def uses_game_only_stats(self) -> bool:
        """dd/td are only derivable per game, so averages cannot be scored directly."""
        return any(k in self.weights for k in ("dd", "td"))

    @property
    def is_default(self) -> bool:
        return self.weights == {k: float(v) for k, v in DEFAULT_POINT_WEIGHTS.items()}

    def __repr__(self):
        return f"PointsScoring({self.weights})"


DEFAULT_POINTS = PointsScoring(DEFAULT_POINT_WEIGHTS)
