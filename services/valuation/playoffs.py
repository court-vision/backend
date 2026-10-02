"""
Fantasy playoffs: which weeks a league's title is decided in, and how many of
them each NBA team plays.

A player's regular season decides whether a team makes the playoffs; his
playoff weeks decide whether it wins them. Three weeks in late March are worth
more than any three in December, and the NBA schedule is not even across them:
in 2026-27's weeks 20-23 Phoenix and Dallas play 16 games, the Lakers and
Cleveland 13. This module answers the two facts everything else needs — the
weeks, and each team's games in them — and nothing else. Weighting them is the
valuation's job.

**Which weeks.**

- ESPN: the matchup periods after `matchupPeriodCount` are the playoffs,
  `ceil(log2(playoffTeamCount))` rounds of them. `scheduleSettings.matchupPeriods`
  maps every period, playoffs included, to its weekly scoring-period ids — a
  two-week round is `{"20": [20, 21]}` — and those ids are the calendar's week
  numbers (`schedule_service`), so the union is the answer. When a period is
  missing from the map the rounds are laid end to end from the first playoff
  week, `playoffMatchupPeriodLength` weeks each.
- Yahoo: `playoff_start_week` through `end_week`. Yahoo's week ids are read as
  the calendar's; the All-Star fortnight may number them differently, which
  `PlayoffWindow.source` flags as `yahoo_weeks_assumed`.
- No league, or one whose settings never synced: ESPN's default shape, read
  off a rolled 2026-27 league (426893737) — the regular season ends a round
  before the calendar does, and two two-week rounds finish the week before the
  NBA's last. With 24 calendar weeks that is weeks 20-23; the final week is
  unplayed, which is exactly when contenders rest and tankers shut down.

**Light nights.** A game on a night with at most `LIGHT_NIGHT_MAX_GAMES` NBA
games is one a daily-lineup manager can nearly always start; a game on a
twelve-game Wednesday competes with the whole roster. The count is shown, not
weighed: the congestion term already prices collisions for the manager's
actual roster.

Everything here is pure: no database, no calendar reads (the caller passes the
weeks in), no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, log2
from typing import Any, Mapping, Optional, Sequence

from services.draft_congestion import SampleWeek

# ESPN's default when a league tells us nothing: two rounds of two weeks each,
# ending one week before the calendar does.
DEFAULT_ROUNDS = 2
DEFAULT_ROUND_WEEKS = 2
DEFAULT_WEEKS_UNPLAYED = 1

# A night with this many NBA games or fewer is a light night (7 games = 14 of 30 teams).
LIGHT_NIGHT_MAX_GAMES = 7


@dataclass(frozen=True)
class PlayoffWindow:
    """The calendar weeks a league's playoffs are played in."""

    weeks: tuple[int, ...]
    source: str                 # league | espn_default | yahoo_weeks_assumed
    rounds: int = 0
    round_weeks: tuple[tuple[int, ...], ...] = ()

    @property
    def label(self) -> str:
        if not self.weeks:
            return "no playoff weeks"
        first, last = min(self.weeks), max(self.weeks)
        return f"week {first}" if first == last else f"weeks {first}-{last}"


@dataclass(frozen=True)
class TeamPlayoffGames:
    """One NBA team's schedule inside a playoff window."""

    team: str
    games: int
    light: int                                  # games on light nights
    per_week: tuple[int, ...] = ()              # games in each playoff week, window order
    back_to_backs: int = 0


@dataclass(frozen=True)
class PlayoffSchedule:
    """Every team's playoff-window games, and the league-wide range they sit in."""

    window: PlayoffWindow
    teams: Mapping[str, TeamPlayoffGames] = field(default_factory=dict)

    def games(self, team: Optional[str]) -> Optional[int]:
        t = self.teams.get(team) if team else None
        return t.games if t is not None else None

    @property
    def mean_games(self) -> float:
        if not self.teams:
            return 0.0
        return sum(t.games for t in self.teams.values()) / len(self.teams)

    @property
    def span(self) -> tuple[int, int]:
        if not self.teams:
            return (0, 0)
        counts = [t.games for t in self.teams.values()]
        return (min(counts), max(counts))


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_window(calendar_weeks: int) -> PlayoffWindow:
    """ESPN's default shape laid over a calendar of `calendar_weeks` weeks."""
    last = calendar_weeks - DEFAULT_WEEKS_UNPLAYED
    first = last - DEFAULT_ROUNDS * DEFAULT_ROUND_WEEKS + 1
    if calendar_weeks <= 0 or first < 1:
        return PlayoffWindow(weeks=(), source="espn_default")
    rounds = tuple(
        tuple(range(first + r * DEFAULT_ROUND_WEEKS, first + (r + 1) * DEFAULT_ROUND_WEEKS))
        for r in range(DEFAULT_ROUNDS)
    )
    return PlayoffWindow(
        weeks=tuple(range(first, last + 1)), source="espn_default",
        rounds=DEFAULT_ROUNDS, round_weeks=rounds,
    )


def playoff_window(
    matchup_periods: Optional[Mapping[str, Any]],
    provider: Optional[str],
    calendar_weeks: int,
) -> PlayoffWindow:
    """The league's playoff weeks, from its synced `matchup_periods`.

    Falls back to ESPN's default whenever the settings cannot answer — no
    league, never synced, a playoff team count below two, or weeks that fall
    outside the calendar — so a caller always gets a window it can show.
    """
    mp = matchup_periods or {}
    if provider == "yahoo":
        start = _int(mp.get("playoff_start_week"))
        end = _int(mp.get("end_week")) or calendar_weeks
        weeks = tuple(w for w in range(start or 0, (end or 0) + 1) if 1 <= w <= calendar_weeks) if start else ()
        if weeks:
            return PlayoffWindow(weeks=weeks, source="yahoo_weeks_assumed",
                                 rounds=len(weeks), round_weeks=tuple((w,) for w in weeks))
        return default_window(calendar_weeks)

    regular = _int(mp.get("period_count"))
    teams = _int(mp.get("playoff_team_count"))
    if not regular or not teams or teams < 2:
        return default_window(calendar_weeks)
    rounds = ceil(log2(teams))
    length = max(_int(mp.get("playoff_period_length")) or 1, 1)
    periods: Mapping[str, Any] = mp.get("periods") or {}

    round_weeks: list[tuple[int, ...]] = []
    # Where a period is missing from the map, lay rounds end to end after the
    # last week the map does give (or after the regular season's periods).
    known_last = max(
        (w for p, ws in periods.items() if (_int(p) or 0) <= regular for w in (ws or []) if _int(w)),
        default=regular,
    )
    cursor = int(known_last) + 1
    for r in range(rounds):
        period = str(regular + 1 + r)
        mapped = tuple(sorted(int(w) for w in (periods.get(period) or []) if _int(w) is not None))
        if not mapped:
            mapped = tuple(range(cursor, cursor + length))
        round_weeks.append(mapped)
        cursor = max(mapped) + 1

    weeks = tuple(w for rw in round_weeks for w in rw)
    if not weeks or min(weeks) < 1 or max(weeks) > calendar_weeks:
        return default_window(calendar_weeks)
    return PlayoffWindow(weeks=weeks, source="league", rounds=rounds, round_weeks=tuple(round_weeks))


def playoff_schedule(window: PlayoffWindow, weeks: Sequence[SampleWeek]) -> PlayoffSchedule:
    """Each team's games, light-night games and back-to-backs inside `window`.

    `weeks` are calendar weeks as per-day team sets (`week_from_calendar`); any
    not in the window are ignored, so the caller may pass the whole season.
    Back-to-backs are counted across week boundaries inside the window.
    """
    by_number = {w.number: w for w in weeks}
    wanted = [by_number[n] for n in window.weeks if n in by_number]
    if not wanted:
        return PlayoffSchedule(window=window, teams={})

    teams: set[str] = set()
    for w in wanted:
        for day in w.days:
            teams |= day

    games: dict[str, int] = {t: 0 for t in teams}
    light: dict[str, int] = {t: 0 for t in teams}
    per_week: dict[str, list[int]] = {t: [] for t in teams}
    b2b: dict[str, int] = {t: 0 for t in teams}
    yesterday: frozenset[str] = frozenset()
    for w in wanted:
        week_games = {t: 0 for t in teams}
        for day in w.days:
            is_light = len(day) // 2 <= LIGHT_NIGHT_MAX_GAMES
            for t in day:
                games[t] += 1
                week_games[t] += 1
                if is_light:
                    light[t] += 1
                if t in yesterday:
                    b2b[t] += 1
            yesterday = day
        for t in teams:
            per_week[t].append(week_games[t])

    return PlayoffSchedule(
        window=window,
        teams={
            t: TeamPlayoffGames(team=t, games=games[t], light=light[t],
                                per_week=tuple(per_week[t]), back_to_backs=b2b[t])
            for t in sorted(teams)
        },
    )
