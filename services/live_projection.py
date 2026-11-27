"""
The live projection, as the boards read it.

Every morning of the season the data platform reprices every player (its
`live_projection` pipeline: skill per minute, role by who is out, a chance for
every game left) and stores the answer in nba.player_projections under the
source `cv_live`: his line in a game he plays from here on, and in `raw` a
timeline of his team's remaining games, one run of alike games per entry:

    [first day, last day, games, chance he plays, minutes, share of the offense]

The timeline is dated, so a snapshot answers for any window that starts on or
after its morning: the games from today on, this week, the fantasy playoffs.
That is what this module does with it. `games_by_week` walks his team's game
days and gives each the chance of the run it falls in, so a player who is out
until January has his games where they will be played, late, and not spread
evenly over the calendar.

A board reads the newest snapshot no older than `FRESH_DAYS` before its own
first day; with none, it keeps the blend it used before
(`services.draft_in_season`). `MODEL` names the valuation built on it, as
`draft_in_season.MODEL` names the blend's.

`load` is the one read; everything else is pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS

if TYPE_CHECKING:
    from services.draft_in_season import DatedWeek

SOURCE = "cv_live"
# The same projection with the analyst's notes applied (the Live rankings plan,
# step 4): built beside the live one each morning, read by the shadow boards alone
SHADOW_SOURCE = "cv_live_shadow"
MODEL = "three-part-1"
# A board row valued by it (`value_source`, and `line_source` on the daily boards)
SOURCE_LABEL = "live"

# The stats the role part moves with his share of the offense (the data platform's
# `live_projection_model.USAGE_LINE_KEYS`); the rest move with his minutes alone
USAGE_KEYS = frozenset(("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "ast", "tov"))
_LINE_FIELDS = frozenset(f.name for f in fields(StatLine))

# How old a snapshot may be and still price a board. Yesterday's is what a
# board opened before this morning's run reads; past two days the job has been
# failing and the blend, which reads this season's games directly, is nearer.
FRESH_DAYS = 2


@dataclass(frozen=True)
class Run:
    """A stretch of his team's games he is expected to play alike."""

    first: date
    last: date
    games: int                              # his team's games in the run
    chance: float                           # that he plays each of them
    # His minutes in a game of the run and his share of the offense against his
    # usual one (1.0: as usual). None on a timeline stored without them.
    minutes: Optional[float] = None
    usage: float = 1.0


@dataclass(frozen=True)
class LivePlayer:
    """One player's row of a live snapshot."""

    player_id: int
    name: str
    espn_id: Optional[int]
    name_normalized: Optional[str]
    team: Optional[str]                     # the team the projection placed him on
    line: StatLine                          # per game, in a game he plays
    games: float                            # games expected from the snapshot's own morning
    timeline: tuple[Run, ...]
    # What each part of the projection read about him (`raw.parts`): an absence
    # and its return date, a note in force, his minutes lately. The reason a
    # board shows beside his rank is read from it (`services.draft_live`).
    parts: Mapping = field(default_factory=dict)
    # Per minute, at his usual share of the offense (`raw.rates`): with a run's
    # minutes and usage, his line in a game of that run (`line_on`)
    rates: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveSnapshot:
    """One morning's live projection."""

    as_of: date
    version: Optional[str]                  # the coefficients that built it
    players: dict[int, LivePlayer]


def parse_timeline(raw: object) -> tuple[Run, ...]:
    """A stored timeline as runs, oldest first. An entry that cannot be read is
    left out: a board prices what it can rather than failing on one row."""
    runs: list[Run] = []
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        try:
            first, last = date.fromisoformat(str(entry[0])), date.fromisoformat(str(entry[1]))
            games, chance = int(entry[2]), float(entry[3])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if last < first or games <= 0:
            continue
        runs.append(Run(first=first, last=last, games=games, chance=min(max(chance, 0.0), 1.0),
                        minutes=_minutes(entry), usage=_usage(entry)))
    runs.sort(key=lambda r: r.first)
    return tuple(runs)


def _minutes(entry: Sequence) -> Optional[float]:
    try:
        minutes = float(entry[4])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return minutes if minutes >= 0 else None


def _usage(entry: Sequence) -> float:
    try:
        usage = float(entry[5])
    except (IndexError, KeyError, TypeError, ValueError):
        return 1.0
    return usage if usage > 0 else 1.0


def run_on(timeline: Sequence[Run], day: date) -> Optional[Run]:
    """The run a game on `day` falls in.

    A game on a day no run covers (the schedule moved since the snapshot) takes
    the nearest earlier run, and the first run for a day before any of them:
    the data platform's own rule (`live_projection_model.expand`).
    """
    chosen: Optional[Run] = None
    for run in timeline:
        if run.first > day:
            break
        chosen = run
    if chosen is None:
        chosen = timeline[0] if timeline else None
    return chosen


def chance_on(timeline: Sequence[Run], day: date) -> float:
    """The chance he plays a game on `day`: that of the run the day falls in (`run_on`)."""
    chosen = run_on(timeline, day)
    return chosen.chance if chosen is not None else 0.0


def run_line(player: LivePlayer, run: Optional[Run]) -> StatLine:
    """His line in a game of `run`, if he plays it: his per-minute rates over the
    run's minutes, the stats that follow the ball scaled by its share of the
    offense (the data platform's `LiveProjection.game_line`). His line over the
    rest of the season stands in where the snapshot carries no rates or the run
    no minutes: one stored before either was read, or a player with no game left."""
    if run is None or run.minutes is None or not player.rates:
        return player.line
    out = {}
    for key, rate in player.rates.items():
        if key in _LINE_FIELDS and key != "min":
            out[key] = float(rate) * run.minutes * (run.usage if key in USAGE_KEYS else 1.0)
    return StatLine(min=run.minutes, gp=1.0, **out)


def line_on(player: LivePlayer, day: date) -> StatLine:
    """His line in a game on `day`, if he plays it (`run_line` of the day's run)."""
    return run_line(player, run_on(player.timeline, day))


def games_by_week(timeline: Sequence[Run], days: Sequence[tuple[date, int]]) -> dict[int, float]:
    """His expected games in each fantasy week, over his team's game days
    (`days`: each game's date and the number of the week it falls in)."""
    out: dict[int, float] = {}
    if not timeline:
        return out
    for day, week in days:
        out[week] = out.get(week, 0.0) + chance_on(timeline, day)
    return out


def games_from(timeline: Sequence[Run], first_day: date) -> float:
    """His expected games from `first_day` on by the timeline's own count, for a
    player whose team the calendar does not carry: each run's games at its
    chance, a run already under way counted by the share of its days left."""
    total = 0.0
    for run in timeline:
        if run.last < first_day:
            continue
        span = (run.last - run.first).days + 1
        left = (run.last - max(run.first, first_day)).days + 1
        total += run.chance * run.games * left / span
    return total


def team_days(weeks: Sequence["DatedWeek"], first_day: date) -> dict[str, list[tuple[date, int]]]:
    """Each team's game days from `first_day` on, each with the number of the
    fantasy week it falls in, off the dated calendar."""
    out: dict[str, list[tuple[date, int]]] = {}
    for dated in weeks:
        for index, teams in enumerate(dated.week.days):
            day = dated.start + timedelta(days=index)
            if day < first_day:
                continue
            for team in teams:
                out.setdefault(team, []).append((day, dated.week.number))
    return out


@dataclass
class Applied:
    """What `apply` priced, for the board's inputs."""

    as_of: date
    version: Optional[str]
    # Every player the snapshot values: his expected games from the board's
    # first day on, and the same by fantasy week (empty for a player whose
    # team the calendar does not carry, who is then spread evenly).
    games_left: dict[int, float] = field(default_factory=dict)
    week_games: dict[int, dict[int, float]] = field(default_factory=dict)


def apply(
    pool: dict[int, PoolRow],
    source: dict[int, str],
    snapshot: LiveSnapshot,
    first_day: date,
    weeks: Sequence["DatedWeek"],
) -> Applied:
    """Value `pool` on `snapshot`, in place: every player it carries takes its
    line and is marked `live` in `source`; one the pool did not have is added.
    A pool row the snapshot does not carry is left as it was.

    His games are priced on the game days of the team the projection placed him
    on, which is the schedule his timeline was built for.
    """
    days = team_days(weeks, first_day)
    out = Applied(as_of=snapshot.as_of, version=snapshot.version)
    for pid, live in snapshot.players.items():
        on = days.get(live.team) if live.team else None
        if on is not None:
            by_week = games_by_week(live.timeline, on)
            games = sum(by_week.values())
        else:
            by_week = {}
            games = games_from(live.timeline, first_day)
        fpts = round(DEFAULT_POINTS.score(live.line), 1)
        had = pool.get(pid)
        if had is not None:
            pool[pid] = replace(
                had, line=live.line, fpts_avg=fpts, fpts_total=round(fpts * max(had.gp, 1), 1),
                team=live.team or had.team, season=None,
            )
        else:
            gp = int(round(games))
            pool[pid] = PoolRow(
                id=pid, name=live.name, team=live.team, gp=gp, line=live.line,
                fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
                espn_id=live.espn_id, name_normalized=live.name_normalized,
            )
        source[pid] = SOURCE_LABEL
        out.games_left[pid] = round(games, 2)
        out.week_games[pid] = by_week
    return out


def _rates(raw: object) -> dict[str, float]:
    """Stored per-minute rates, the ones that read as numbers."""
    out: dict[str, float] = {}
    for key, value in (raw.items() if isinstance(raw, Mapping) else ()):
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def load(season: str, day: date, fresh_days: int = FRESH_DAYS, source: str = SOURCE) -> Optional[LiveSnapshot]:
    """The newest live snapshot a board counting from `day` may read, or None.
    `source` is whose: the live projection's unless the shadow one is asked for."""
    from db.models.nba.player_projections import PlayerProjection
    from db.models.nba.players import Player

    of_season = (PlayerProjection.season == season) & (PlayerProjection.source == source)
    latest = (
        PlayerProjection.select(PlayerProjection.as_of_date)
        .where(of_season
               & (PlayerProjection.as_of_date <= day)
               & (PlayerProjection.as_of_date >= day - timedelta(days=fresh_days)))
        .order_by(PlayerProjection.as_of_date.desc())
        .limit(1)
        .scalar()
    )
    if latest is None:
        return None
    players: dict[int, LivePlayer] = {}
    version: Optional[str] = None
    records = (
        PlayerProjection.select(PlayerProjection, Player)
        .join(Player)
        .where(of_season & (PlayerProjection.as_of_date == latest))
    )
    for rec in records:
        raw: Mapping = rec.raw if isinstance(rec.raw, Mapping) else {}
        version = version or (str(raw["version"]) if raw.get("version") else None)
        try:
            games = float(raw.get("games"))
        except (TypeError, ValueError):
            games = float(rec.projected_gp or 0)
        players[rec.player_id] = LivePlayer(
            player_id=rec.player_id, name=rec.player.name, espn_id=rec.player.espn_id,
            name_normalized=rec.player.name_normalized,
            team=str(raw["team"]) if raw.get("team") else None,
            line=StatLine.from_row(rec), games=games, timeline=parse_timeline(raw.get("timeline")),
            parts=raw["parts"] if isinstance(raw.get("parts"), Mapping) else {},
            rates=_rates(raw.get("rates")),
        )
    if not players:
        return None
    return LiveSnapshot(as_of=latest, version=version, players=players)
