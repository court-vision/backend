"""
A player's price over the window a decision is about (the Live rankings plan, step 8).

The morning's live projection (`services.live_projection`) dates every game a player
has left: the chance he plays it, his minutes and his share of the offense that night.
So the same snapshot prices him for any window from its morning on, and a decision can
read the window it is about:

- a pickup or a stream is priced on its own window: tonight, or the days left in the
  matchup week (`LivePrice.days`, one figure per game day);
- the player given up is priced on the rest of the season (`LivePrice.season`),
  because a drop cannot be taken back. A slow fortnight, or a week on the injury
  report, moves that price very little; a role that ends when a starter returns
  counts only for the games it lasts.

Every figure is in expected points under the league's scoring: his line in that
game times the chance he plays it. So a questionable player's night is worth less
than a healthy one's, and one who is out is worth nothing until his return date.

Pure: no database and no clock. `services.live_price_service` reads the snapshot
and the calendar and calls `price`.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Mapping, Optional, Sequence

from services.live_projection import LivePlayer, run_line
from services.scoring.models import StatLine

DECIMALS = 2


@dataclass(frozen=True)
class Window:
    """A player priced over a run of days."""

    value: float                            # expected points over the window
    games: float                            # the games he is expected to play in it
    team_games: int                         # his team's games in it

    @property
    def per_game(self) -> Optional[float]:
        """Expected points per game on his team's schedule: a game he is likely
        to miss counts for that much less. None over a window with no games."""
        return round(self.value / self.team_games, DECIMALS) if self.team_games else None


@dataclass(frozen=True)
class LivePrice:
    """One player priced from a day on."""

    # expected points on each of his team's game days, from the first day priced
    # through the reach asked for (the days a stream or a week is priced on)
    days: Mapping[date, float] = field(default_factory=dict)
    # the rest of the season: every game his team has left on the calendar
    season: Window = Window(0.0, 0.0, 0)

    def on(self, day: date) -> Optional[float]:
        """Expected points on `day`; None when his team does not play it (or the
        day is past the reach priced by the day)."""
        return self.days.get(day)

    def over(self, first: date, last: date) -> float:
        """Expected points from `first` to `last`, both included."""
        return round(sum(v for d, v in self.days.items() if first <= d <= last), DECIMALS)


def price(player: LivePlayer, score: Callable[[StatLine], float], game_days: Sequence[date],
          reach: date) -> Optional[LivePrice]:
    """`player` priced over `game_days`: his team's game days from the first day
    priced on, in order, to the end of the calendar. `score` is the league's
    points for a line. Days through `reach` are priced one by one.

    None when there is nothing to price him on: no timeline, or no game left.

    Each game takes the run of the timeline it falls in, the nearest earlier one
    when the schedule has moved since the snapshot (`live_projection.run_on`), so
    the runs cut `game_days` into stretches and a window is a sum over them.
    """
    timeline = player.timeline
    if not timeline or not game_days:
        return None
    # the index of the first game day of each run's stretch; the first run also
    # answers for days before it
    starts = [0] + [bisect_left(game_days, run.first) for run in timeline[1:]]
    ends = starts[1:] + [len(game_days)]
    worth = [run.chance * max(float(score(run_line(player, run))), 0.0) for run in timeline]

    value = games = 0.0
    for run, start, end, each in zip(timeline, starts, ends, worth):
        n = end - start
        if n > 0:
            games += n * run.chance
            value += n * each

    days: dict[date, float] = {}
    at = 0
    for i in range(bisect_right(game_days, reach)):
        while at + 1 < len(starts) and starts[at + 1] <= i:
            at += 1
        days[game_days[i]] = round(worth[at], DECIMALS)

    return LivePrice(days=days, season=Window(round(value, DECIMALS), round(games, DECIMALS), len(game_days)))
