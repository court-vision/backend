"""
The daily boards: Court Vision's two standard rankings as of this morning.

The Draft lab ranks every player whenever a board is opened, and until now the
answer was never kept. The Live rankings plan starts by keeping it: every
morning the data platform asks for the two standard boards (points and 9-cat),
stores them in nba.daily_rankings beside ESPN's rank that day, and from then on
a rank has a history: how it moved, when ESPN last moved its own, and which of
the two was right about what happened next.

The boards are the ones a room with no league shows today, not a second
opinion: `DraftBoardService._fetch_inputs` with no room and no picks, ranked by
`services.valuation.standard.rank_boards`. In season that is the morning's live
projection (`services.live_projection`: the data platform builds it just before
it asks for these boards), each player's line and the games he is expected to
play from here, a missed game charged at the gap to a replacement. On a morning
with no fresh one it is the preseason projection blended with this season's
games (`services.draft_in_season`), and before opening night the projection
alone. `MODEL_*` names which, and is stored with every row, so the day a better
model takes over is visible in the history instead of looking like a hundred
players changing their minds at once.

`build` is pure and `today` is the route's whole job (`POST
/v1/internal/jobs/rankings/daily`): one read, then the valuation. Nothing here
writes: the data platform owns the nba.* tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.compute import run_cpu
from db import base as db_base
from services import draft_in_season, live_projection, schedule_service
from services.draft_board_service import BoardInputs, DraftBoardService
from services.valuation.engine import DEFAULT_PLAYOFF_WEIGHT, Valued
from services.valuation.standard import rank_boards

# What built the board. A change to how a line or its games are made is a new name.
MODEL_LIVE = live_projection.MODEL      # the live projection: skill, role, a chance for each game left
MODEL_IN_SEASON = draft_in_season.MODEL  # the projection blended with this season's games, games left
MODEL_PRESEASON = "projection-1"        # the projection alone, a full season of games


@dataclass(frozen=True)
class BoardPlace:
    """One player's place on one board."""

    rank: int
    value: float            # the per-game number the board shows: fantasy points, or the 9-cat index
    score: float            # what the rank is ordered by
    games: float            # the games he is expected to play from here


@dataclass(frozen=True)
class DailyRank:
    """One player on both boards, with what the board knew about him."""

    player_id: int
    name: str
    team: Optional[str]
    line_source: str                    # live | projection | season | baseline: where his line came from
    season_gp: Optional[int]            # games played this season; None before opening night
    injury_status: Optional[str]        # today's flag in the board's vocabulary; None plays
    points: BoardPlace
    categories: BoardPlace


@dataclass(frozen=True)
class DailyBoards:
    """Both standard boards for one day."""

    season: str
    as_of: date                         # the fantasy day: games from this day on are counted
    in_season: bool
    model: str
    season_as_of: Optional[date]        # the newest box score the lines read
    projections_as_of: Optional[date]
    projection_source: Optional[str]
    prior_games: int                    # the projection's weight, in games of this season
    playoff_weight: float
    players: list[DailyRank]            # in points-board order
    live_as_of: Optional[date] = None   # the morning of the live projection behind it; None on the blend


def _points_place(rank: int, valued: Valued) -> BoardPlace:
    return BoardPlace(rank=rank, value=valued.value, score=valued.season_value,
                      games=round(valued.expected_games, 2))


def _category_place(rank: int, valued: Valued) -> BoardPlace:
    return BoardPlace(rank=rank, value=valued.value, score=valued.z_sum or 0.0,
                      games=round(valued.expected_games, 2))


def build(inputs: BoardInputs, today: date, playoff_weight: float = DEFAULT_PLAYOFF_WEIGHT) -> DailyBoards:
    """The two standard boards for `inputs`, the stateless board's read.

    `today` dates a board built outside the season; in season the board's own
    first counted day does.
    """
    points, categories = rank_boards(inputs, playoff_weight)
    found = inputs.in_season
    players: list[DailyRank] = []
    for row in inputs.pool:
        points_rank, by_points = points[row.id]
        category_rank, by_category = categories[row.id]
        players.append(DailyRank(
            player_id=row.id,
            name=row.name,
            team=inputs.current_team.get(row.id) or row.team,
            line_source=inputs.source.get(row.id, "baseline"),
            season_gp=found.season_gp.get(row.id, 0) if found is not None else None,
            injury_status=DraftBoardService._injury_for(inputs, row.id),
            points=_points_place(points_rank, by_points),
            categories=_category_place(category_rank, by_category),
        ))
    players.sort(key=lambda p: p.points.rank)
    return DailyBoards(
        season=inputs.season,
        as_of=found.games_from if found is not None else today,
        in_season=found is not None,
        model=found.model if found is not None else MODEL_PRESEASON,
        live_as_of=found.live_as_of if found is not None else None,
        season_as_of=found.season_as_of if found is not None else None,
        projections_as_of=inputs.projections_as_of,
        projection_source=inputs.projection_source,
        prior_games=draft_in_season.SEASON_PRIOR_GAMES,
        playoff_weight=playoff_weight,
        players=players,
    )


def load_inputs(live_source: str = live_projection.SOURCE) -> BoardInputs:
    """The stateless board's inputs: no room, no picks. One trip to the database."""
    return DraftBoardService._fetch_inputs(frozenset(), None, live_source)


async def today(shadow: bool = False) -> DailyBoards:
    """This morning's boards: the read in the database pool, the valuation off the event loop.

    `shadow` builds them on the projection that has the analyst's notes applied
    (`live_projection.SHADOW_SOURCE`; the Live rankings plan, step 4). Nothing a
    user sees reads those: the data platform stores them beside the day's own
    boards so the two can be graded against what happened next. With no shadow
    projection for the day the boards come back on the blend, and `model` and
    `live_as_of` say so: the caller stores nothing then.
    """
    source = live_projection.SHADOW_SOURCE if shadow else live_projection.SOURCE
    inputs = await db_base.run_db("rankings.daily.fetch", load_inputs, source)
    return await run_cpu("rankings.daily.build", build, inputs, schedule_service.get_nba_today())
