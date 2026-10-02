"""
The standard league: where a projection ranks for nobody's league in particular.

The projections editor on the data platform shows, beside every projected line,
the player's rank in standard points and in standard 9-cat, and previews how an
edit would move it. Those ranks have to be the ones a drafter would see, so they
are not re-derived anywhere else: the editor sends the projections here and
they are valued by `DraftBoardService.rank_pool` — the same ladder the board,
the mock autopicker and the recap read.

"Standard" is what a room with no league gets: ESPN's default points weights
or the standard nine categories, twelve teams, thirteen rounds, ESPN's default
fantasy-playoff weeks on this season's calendar, and the default playoff
weight. A `cv_rank` in such a room, for the same projections, is the rank
returned here.

Pure once the calendar is read: no database, and nothing is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from services.draft_board_service import BoardInputs, BoardSession, DraftBoardService
from services.draft_congestion import SampleWeek
from services.scoring.category_rank import PoolRow
from services.scoring.category_value import rankable_categories
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.resolver import resolve_scoring
from services.valuation.engine import DEFAULT_PLAYOFF_WEIGHT, Valued
from services.valuation.playoffs import playoff_window

STANDARD_LEAGUE_SIZE = 12
STANDARD_ROUNDS = 13


def season_calendar() -> tuple[SampleWeek, ...]:
    """This season's fantasy weeks — the static calendar every board reads.
    Empty when it is not on disk; the valuation then counts plain games."""
    return DraftBoardService._calendar_weeks()


def standard_playoff_weeks(calendar: Sequence[SampleWeek]) -> tuple[int, ...]:
    """The fantasy-playoff weeks a league-less room is given on `calendar`."""
    return tuple(playoff_window(None, None, len(calendar)).weeks) if calendar else ()


@dataclass(frozen=True)
class ProjectedLine:
    """One player's projection as the editor holds it."""

    player_id: int
    line: Mapping[str, float]               # per-game stats, canonical keys
    games: Optional[float] = None           # expected games; None is "nobody projects them"
    team: Optional[str] = None              # current NBA team, for the schedule split
    dd_rate: Optional[float] = None
    td_rate: Optional[float] = None


@dataclass(frozen=True)
class StandardRank:
    """Where one projection lands in the standard league, both formats."""

    player_id: int
    points_rank: int
    points_value: float                     # fantasy points per game, ESPN default weights
    points_season: float                    # what the points rank is ordered by
    category_rank: int
    category_value: float                   # the 9-cat index
    category_score: float                   # summed per-category score behind it
    games: float                            # effective games in the standard league


def _inputs(lines: Sequence[ProjectedLine], calendar: Sequence[SampleWeek]) -> BoardInputs:
    pool: list[PoolRow] = []
    projected_gp: dict[int, Optional[int]] = {}
    game_rates: dict[int, tuple[float, float]] = {}
    current_team: dict[int, Optional[str]] = {}
    for p in lines:
        line = StatLine.from_dict(p.line)
        fpts = round(DEFAULT_POINTS.score(line), 1)
        gp = int(round(p.games)) if p.games is not None else 0
        pool.append(PoolRow(
            id=p.player_id, name=str(p.player_id), team=p.team, gp=gp, line=line,
            fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
        ))
        projected_gp[p.player_id] = gp or None
        current_team[p.player_id] = p.team
        if p.dd_rate is not None or p.td_rate is not None:
            game_rates[p.player_id] = (float(p.dd_rate or 0.0), float(p.td_rate or 0.0))
    return BoardInputs(
        season="", pool=pool, projected_gp=projected_gp, game_rates=game_rates,
        current_team=current_team, calendar=tuple(calendar),
        source={row.id: "projection" for row in pool},
    )


def standard_ranks(
    lines: Sequence[ProjectedLine],
    calendar: Sequence[SampleWeek] = (),
    playoff_weight: float = DEFAULT_PLAYOFF_WEIGHT,
) -> list[StandardRank]:
    """Rank `lines` in the standard league, in points and in 9-cat.

    One entry per line, in the order given. A duplicate player id keeps its
    last line: the pool is keyed by player.
    """
    unique = list({p.player_id: p for p in lines}.values())
    inputs = _inputs(unique, calendar)
    session = BoardSession(
        league_size=STANDARD_LEAGUE_SIZE, rounds=STANDARD_ROUNDS, playoff_weight=playoff_weight
    )

    points_scoring = resolve_scoring(None)
    points: dict[int, tuple[int, Valued]] = {
        entry.row.id: (rank, entry)
        for rank, entry in enumerate(
            DraftBoardService.rank_pool(points_scoring, inputs, [], session), start=1
        )
    }
    category_scoring = resolve_scoring(None, "categories")
    categories: dict[int, tuple[int, Valued]] = {
        entry.row.id: (rank, entry)
        for rank, entry in enumerate(
            DraftBoardService.rank_pool(
                category_scoring, inputs, rankable_categories(category_scoring), session
            ),
            start=1,
        )
    }

    out: list[StandardRank] = []
    for p in lines:
        points_rank, by_points = points[p.player_id]
        category_rank, by_category = categories[p.player_id]
        out.append(StandardRank(
            player_id=p.player_id,
            points_rank=points_rank,
            points_value=by_points.value,
            points_season=by_points.season_value,
            category_rank=category_rank,
            category_value=by_category.value,
            category_score=by_category.z_sum or 0.0,
            games=by_points.games,
        ))
    return out
