"""
The draft board in season: a draft held after opening night.

The board was built for draft prep. Its values come from a preseason projection
(or last season's line), its games from a full season's schedule, and its
injury flags from ESPN's draft-market snapshot, all of which stop updating on
Oct 31 (data-platform's `preseason_market_window`). A league that drafts in
November would draft on October's opinion of every player and price a full
season of games, weeks already played included.

This module holds what changes once a draft starts after opening night. The
season's box scores, the live injury feed and the calendar are already in the
database; only the board never read them.

- **This season's games.** Each player's per-game line is blended with this
  season's per-game line, weighted by games played against
  `SEASON_PRIOR_GAMES` of the prior's:

      line = (n · this_season + K · prior) / (n + K)

  K was fitted on eight seasons (2017-18 to 2025-26, 2020-21 left out for its
  December start) with last season's line as the prior. It predicted each
  player's rest-of-season fantasy points per game from his games to a cutoff,
  for the top 200 by last season's line, with at least 10 games on each side.
  K = 10 was best at every cutoff tried (26, 45 and 70 days after opening night;
  anything from 8 to 12 is within 0.01 of it). At 26 days, about where
  2026-27 is on Nov 15, the error was 3.72 fantasy points per game, against
  4.65 for last season's line alone and 4.58 for this season's alone. The
  board's prior is usually Court Vision's projection, which beat last season's
  line by about 5% in its own backtest, so the same K is a slightly cautious
  weight on this season. It is one weight for the whole line: the backtest
  scored points, and a category's own noise is still priced by the valuation
  engine's G-score.
- **The games left.** The calendar keeps only today and later, so a player's
  value is built on the games his team still plays, and the lineup congestion
  term only charges nights still to come. His expected games are his
  projection's share of a full season applied to what is left.
- **Today's injuries.** The injury flag is today's report (nba.player_injuries,
  from ESPN, refreshed before every slate), not the Oct 31 snapshot. A
  projection prices the injuries known when it was made, so the injury term
  also charges a player whose injury is new since the market snapshot, even
  where his games are projected.

What it can't fix: ESPN's ADP is October's crowd. Availability (whether a player
lasts to the next pick) still reads it, and the board's meta says how old it is.

Which rooms: a live, mock or hand-entered room that started on or after opening
night, and the stateless board in season. An imported room, and a room that
started before opening night, keeps the preseason valuation, so the recap of a
preseason draft still grades it on what was known when it was drafted.

Pure, apart from `load_season_rows` and `load_live_injuries`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from services.draft_congestion import SampleWeek
from services.schedule_service import FANTASY_DAY_ROLLOVER_ET
from services.scoring.category_rank import PoolRow

# The prior's weight, in games of this season (see the module docstring)
SEASON_PRIOR_GAMES = 10

# A player with no projection and no qualifying last season is valued off this
# season alone only once his games outweigh what a prior would have counted
SEASON_ONLY_MIN_GP = SEASON_PRIOR_GAMES

# Games assumed when nothing projects a player's (engine.DEFAULT_GAMES)
DEFAULT_SEASON_GAMES = 65.0

# Rooms that keep the preseason valuation whenever they were made: an import
# records a draft that already happened, on the day it was imported
PRESEASON_ONLY_KINDS = frozenset({"import"})

# nba.player_injuries' vocabulary (data-platform's ESPN_STATUS_MAP) as the
# board's ESPN one. Probable and Available play, so they carry no flag.
_LIVE_TO_ESPN: dict[str, Optional[str]] = {
    "out": "OUT",
    "doubtful": "DOUBTFUL",
    "questionable": "QUESTIONABLE",
    "day-to-day": "DAY_TO_DAY",
    "probable": None,
    "available": None,
}


@dataclass(frozen=True)
class DatedWeek:
    """One fantasy week with the date of its first day."""

    start: date
    week: SampleWeek


@dataclass
class InSeason:
    """What `apply` changed, for the board's rows and meta."""

    games_from: date                                    # the first day counted
    season_gp: dict[int, int] = field(default_factory=dict)        # games played this season
    season_fpts: dict[int, float] = field(default_factory=dict)    # this season's fantasy points per game
    season_as_of: Optional[date] = None                 # the newest season row read
    blended: int = 0                                    # pool rows whose line took this season's games
    season_only: int = 0                                # players valued off this season alone


def games_from(
    today: date,
    opening_night: Optional[date],
    regular_season_end: Optional[date],
    *,
    kind: Optional[str] = None,
    started: Optional[date] = None,
) -> Optional[date]:
    """The day an in-season board counts from, or None for the preseason valuation.

    `started` is when the room started (its first pick, else when it was made);
    None for the stateless board, which goes by today alone.
    """
    if opening_night is None or regular_season_end is None:
        return None
    if not (opening_night <= today <= regular_season_end):
        return None
    if kind in PRESEASON_ONLY_KINDS:
        return None
    if started is not None and started < opening_night:
        return None
    return today


def blend_line(prior: PoolRow, current: PoolRow, k: float = SEASON_PRIOR_GAMES) -> PoolRow:
    """`prior` with this season's per-game line folded in by games played."""
    n = max(int(current.gp or 0), 0)
    if n <= 0:
        return prior
    w = n / (n + k)
    line = prior.line.scaled(1.0 - w) + current.line.scaled(w)
    line.gp = 1.0
    fpts_avg = round((1.0 - w) * prior.fpts_avg + w * current.fpts_avg, 2)
    return replace(
        prior, line=line, fpts_avg=fpts_avg, fpts_total=round(fpts_avg * max(prior.gp, 1), 1),
        team=current.team or prior.team,
    )


def trim_calendar(weeks: Sequence[DatedWeek], first_day: Optional[date]) -> tuple[SampleWeek, ...]:
    """The calendar from `first_day` on. A day before it is dropped, so a week
    already played keeps its number with no days left in it: week numbers place
    the playoffs, and the congestion term scales by the count of weeks."""
    out: list[SampleWeek] = []
    for dw in weeks:
        if first_day is None:
            out.append(dw.week)
            continue
        days = tuple(
            teams for index, teams in enumerate(dw.week.days)
            if dw.start + timedelta(days=index) >= first_day
        )
        out.append(SampleWeek(number=dw.week.number, days=days))
    return tuple(out)


def team_games(weeks: Iterable[SampleWeek]) -> dict[str, int]:
    """Each team's games over `weeks`."""
    counts: dict[str, int] = {}
    for week in weeks:
        for day in week.days:
            for team in day:
                counts[team] = counts.get(team, 0) + 1
    return counts


def games_left(
    projected: Optional[float],
    team: Optional[str],
    full: Mapping[str, int],
    left: Mapping[str, int],
) -> float:
    """His expected games from here: his projection's share of a full season
    (`DEFAULT_SEASON_GAMES` when nothing projects him) times what his team has
    left. A team the calendar doesn't know takes the league's average share."""
    games = float(projected) if projected else DEFAULT_SEASON_GAMES
    if team and full.get(team):
        fraction = left.get(team, 0) / full[team]
    elif full:
        fraction = sum(left.get(t, 0) for t in full) / sum(full.values())
    else:
        return games
    return round(games * fraction, 2)


def live_status(status: Optional[str]) -> Optional[str]:
    """A live report's status in the board's ESPN vocabulary; None for a player who plays."""
    if not status:
        return None
    key = str(status).strip().lower()
    if key in _LIVE_TO_ESPN:
        return _LIVE_TO_ESPN[key]
    return str(status).strip().upper().replace(" ", "_") or None


def apply(
    pool: dict[int, PoolRow],
    source: dict[int, str],
    current: Sequence[PoolRow],
    first_day: date,
    *,
    season_as_of: Optional[date] = None,
    k: float = SEASON_PRIOR_GAMES,
) -> InSeason:
    """Fold this season's lines into `pool` in place (and `source` for players
    valued off this season alone); returns what changed."""
    out = InSeason(games_from=first_day, season_as_of=season_as_of)
    for row in current:
        n = int(row.gp or 0)
        if n <= 0:
            continue
        out.season_gp[row.id] = n
        out.season_fpts[row.id] = round(row.fpts_avg, 1)
        prior = pool.get(row.id)
        if prior is not None:
            pool[row.id] = blend_line(prior, row, k)
            out.blended += 1
        elif n >= SEASON_ONLY_MIN_GP:
            pool[row.id] = replace(row, season=None)
            source[row.id] = "season"
            out.season_only += 1
    return out


# ---- the two reads ----------------------------------------------------------


def load_season_rows(season: str) -> tuple[Optional[date], list[PoolRow]]:
    """This season's per-game rows (each player's latest season row)."""
    from services.scoring.pool import load_pool
    return load_pool(None, season=season)


def load_live_injuries(as_of: date) -> dict[int, str]:
    """Today's injury flag per player, in the board's vocabulary: the newest
    current report of every player who isn't cleared to play."""
    from db.models.nba.player_injuries import PlayerInjury
    out: dict[int, str] = {}
    for report in PlayerInjury.get_injured_players(as_of):
        status = live_status(report.status)
        if status is not None:
            out[int(report.player_id)] = status
    return out


_EASTERN = ZoneInfo("America/New_York")


def fantasy_day(stamp: datetime) -> date:
    """The fantasy day a stored timestamp falls on. Rooms store naive UTC
    (datetime.utcnow()), and the fantasy day turns at 3 AM ET, so a first pick
    at 9 PM ET on Oct 19 is Oct 19, not the Oct 20 its UTC date says."""
    aware = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
    local = aware.astimezone(_EASTERN)
    if local.time() < FANTASY_DAY_ROLLOVER_ET:
        return local.date() - timedelta(days=1)
    return local.date()


def drafted_on(
    started_at: Optional[datetime], created_at: Optional[datetime], status: Optional[str], today: date
) -> Optional[date]:
    """The fantasy day a room's draft happens: its first pick; for a room still
    waiting for one, today (a room made in October for a November draft drafts
    in season); for a finished room that never recorded one, the day it was made."""
    if started_at is not None:
        return fantasy_day(started_at)
    if status == "active":
        return today
    return fantasy_day(created_at) if created_at is not None else None
