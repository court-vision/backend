"""
The what-if projection: today's matchup projection with roster moves applied, and
the drop choice (the CV AI plan, § 5).

Today's projection (`EspnService.get_matchup_data`, the Yahoo build) is the current
totals plus, for each counting player, his per-game line times his games left in
the matchup. A what-if is a change of inputs, not a new model:

- a dropped player stops counting from the move's effective day: his games before
  it stay, the rest go;
- an added player counts from the effective day, for his games from then to the
  matchup's end;
- moves apply in day order to one roster, so a player one move adds can be the
  one a later move drops (a stream: A today, B from day 4).

So `games_remaining` becomes games per day: each player carries the matchup days
(0-based) from today on that his team plays, read off the season calendar the
projection already uses. The caller decides each move's effective day (a move made
after the day's first tip-off counts from the next day: `services.ai.fantasy`).

The drop choice is the daily swap's rule (`daily_actions_service.weigh_drop`) over
the rest of the matchup: the player whose loss costs the least, his weighed per-game
value times the games he would lose, with players short on games weighed at their
season projection and unknowns never dropped.

Known simplification (v1): like today's projection, lineup slots are ignored --
every counting player (off IR, not injured) is assumed to start every game he has.

Pure: no I/O. `services.ai.fantasy` builds the inputs from a matchup read and the
calendar and turns the result into the tool's rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryTeamScoreData, StatLine


class MoveInvalid(ValueError):
    """A move that cannot be applied to the roster as it stands."""


@dataclass(frozen=True)
class SidePlayer:
    """One player as the projection sees him."""

    player_id: Optional[int]           # NBA id; None when the provider's player is unmapped
    name: str
    team: str
    value: float                       # per game: fantasy points, or the category value proxy
    game_days: tuple[int, ...]         # matchup days (0-based), today on, with a game
    counts: bool = True                # off IR and not injured: counts toward the projection
    line: Optional[StatLine] = None    # per game, category leagues

    def games_from(self, day: int, until: Optional[int] = None) -> int:
        """His games on days >= `day` (and < `until`)."""
        return sum(1 for g in self.game_days if g >= day and (until is None or g < until))


@dataclass(frozen=True)
class Move:
    day: int                           # the effective day (0-based matchup day)
    add: Optional[SidePlayer] = None
    drop_id: Optional[int] = None      # NBA id of the roster player dropped


@dataclass(frozen=True)
class Applied:
    """One move as applied: who came in and went out, and the games that changed hands."""

    day: int
    add: Optional[SidePlayer]
    add_games: int                     # games the add counts for (0 when he does not count)
    drop: Optional[SidePlayer]
    drop_games: int                    # games the drop stops counting for


@dataclass(frozen=True)
class WhatIf:
    counted: tuple[tuple[SidePlayer, int], ...]   # every player and the games he counts for after the moves
    moves: tuple[Applied, ...]


@dataclass
class _Holding:
    player: SidePlayer
    start: Optional[int]               # None: on the roster from today
    end: Optional[int] = None          # None: still on the roster at the matchup's end

    def games(self, last_day: int) -> int:
        return sum(1 for g in self.player.game_days
                   if g <= last_day and (self.start is None or g >= self.start) and (self.end is None or g < self.end))


def apply_moves(roster: Sequence[SidePlayer], moves: Sequence[Move], *, last_day: int) -> WhatIf:
    """The roster's games after the moves, applied in day order. A drop must name a player on
    the roster at that point (added by an earlier move or there from the start); an add must
    not be on it. Raises MoveInvalid otherwise."""
    holdings = [_Holding(p, None) for p in roster]
    applied: list[Applied] = []
    for move in sorted(moves, key=lambda m: m.day):
        drop = drop_games = None
        if move.drop_id is not None:
            held = next((h for h in holdings if h.end is None and h.player.player_id == move.drop_id), None)
            if held is None:
                raise MoveInvalid(f"Player {move.drop_id} is not on the roster on day {move.day}")
            held.end = move.day
            drop, drop_games = held.player, held.player.games_from(move.day, last_day + 1)
        add = add_games = None
        if move.add is not None:
            if any(h.end is None and h.player.player_id == move.add.player_id for h in holdings):
                raise MoveInvalid(f"{move.add.name} is already on the roster on day {move.day}")
            holdings.append(_Holding(move.add, move.day))
            add = move.add
            add_games = add.games_from(move.day, last_day + 1) if add.counts else 0
        applied.append(Applied(move.day, add, add_games or 0, drop, drop_games or 0))
    counted: dict[int, tuple[SidePlayer, int]] = {}
    for h in holdings:
        key = id(h.player)
        player, games = counted.get(key, (h.player, 0))
        counted[key] = (player, games + h.games(last_day))
    return WhatIf(tuple(counted.values()), tuple(applied))


def games_counted(roster: Sequence[SidePlayer], *, last_day: int) -> tuple[tuple[SidePlayer, int], ...]:
    """The roster as it stands: every player and his games left (the projection without moves)."""
    return apply_moves(roster, [], last_day=last_day).counted


# ------------------------------- scoring ------------------------------- #


def points_projection(current: float, counted: Iterable[tuple[SidePlayer, int]]) -> float:
    """The current score plus each counting player's value times his games: today's points projection."""
    return round(current + sum(p.value * games for p, games in counted if p.counts), 2)


def category_projection(cats: CategoryScoring, base: CategoryTeamScoreData,
                        counted: Iterable[tuple[SidePlayer, int]]) -> dict[str, float]:
    """The base totals with each counting player's line times his games overlaid: today's category
    projection (`CategoryScoring.project`), rates recomputed from makes and attempts."""
    return cats.project(base, [(p.line or StatLine(), games, p.counts) for p, games in counted])


# ------------------------------- the drop choice ------------------------------- #


@dataclass(frozen=True)
class DropCandidate:
    player: SidePlayer
    weighed: Optional[float]           # the per-game value the rule weighs him at; None = unknown, never dropped
    projected: bool = False            # weighed at his season projection rather than his average


@dataclass(frozen=True)
class DropChoice:
    candidate: DropCandidate
    games_lost: int                    # his games from the move's day to the matchup's end
    cost: float                        # weighed value times games lost


def choose_drop(candidates: Sequence[DropCandidate], *, day: int, last_day: int) -> Optional[DropChoice]:
    """The player whose loss from `day` costs the least: his weighed per-game value times the games he
    would lose, so a player with no game left is the free drop. An injured player keeps his games (a
    drop cannot be taken back, and he may be back before they are played). Unknowns are never dropped.
    Ties: fewer games lost, the lower weighed value, then the name. None when nobody can be dropped."""
    choices: list[DropChoice] = []
    for c in candidates:
        if c.weighed is None:
            continue
        games = c.player.games_from(day, last_day + 1)
        choices.append(DropChoice(c, games, round(c.weighed * games, 4)))
    if not choices:
        return None
    return min(choices, key=lambda d: (d.cost, d.games_lost, d.candidate.weighed, d.candidate.player.name))
