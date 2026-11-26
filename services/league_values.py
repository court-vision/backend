"""
Value over a window (the Analysis desk plan, step 2): every player in a league priced
for any run of its matchups, under the league's own scoring.

A player's worth depends on the weeks it is measured over: value(player, from, to,
league). The same man is worth one thing this week, with four games, and another in
the fantasy playoffs, with nine games and a sprain in between. So each player is
priced over the days of the window still to come, the way the league projection
(`services.league_projection`) reads them:

- `games`: his expected games, each of his NBA team's games in the window at his
  chance to play it (the live projection's timeline). A game already under way today
  (`League.today_left`) counts only for a rostered player it locked in a starting
  slot, and only for what's left of it: a benched or free player can't be moved into
  it any more.
- `value`: his points per game under the league's scoring times those games: what he
  scores if every game he plays counts.
- `started`: for a rostered player, what he adds to his own roster's lineup over the
  window, seated each day in the league's slots as the league projection seats
  rosters (`league_projection.day_seats`). `value - started` sits on the bench.
- `season_value` and the ranks: the same for the rest of the fantasy season, and each
  player's place among every player priced (all the league's rosters and the free
  agents read), so one worth more in the window than over the season shows as such.

The tiers set each roster against the rest of the league: its best player's window
value against the other rosters' best, its second against their second, down the
roster, so a gap shows where it is (the top, the middle, the bench).

A window is a run of matchups (a two-week playoff round counts both weeks), named by a
preset or by its first and last matchup. One under way counts from today.

Pure: no database, no ESPN, no clock. `services.league_values_service` reads the
inputs and calls `price_league`.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from statistics import mean, median
from typing import Mapping, Optional, Sequence

from services import league_projection as lp
from services.draft_congestion import NON_STARTING_SLOTS, active_slots

# this_week: the current matchup; next_4: it and the three after; to_playoffs: the rest of
# the regular season; playoffs: the league's playoff matchups; rest_of_season: all of them
WINDOWS = ("this_week", "next_4", "to_playoffs", "playoffs", "rest_of_season")
DEFAULT_WINDOW = "this_week"
POSITIONS = ("PG", "SG", "SF", "PF", "C")


class WindowError(ValueError):
    """A window the league can't be priced over: its code and a sentence the desk can show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def final_matchup(regular: int, playoff_teams: int, teams: int) -> int:
    """The league's last matchup: the regular season, then a round per halving of the field."""
    return regular + lp.playoff_rounds(min(max(playoff_teams, 0), teams))


def window_range(key: Optional[str], first: Optional[int], last: Optional[int], *,
                 current: int, regular: int, final: int) -> tuple[int, int]:
    """The window's first and last matchup. A first or last matchup given wins over the
    preset (only a first: that one matchup; only a last: from the current one). A window
    under way starts at the current matchup."""
    if first is not None or last is not None:
        lo = first if first is not None else current
        hi = last if last is not None else lo
        if lo > hi:
            raise WindowError("LEAGUE_VALUES_BAD_WINDOW", "The window's first matchup comes after its last")
        if lo < 1 or hi > final:
            raise WindowError("LEAGUE_VALUES_BAD_WINDOW", f"This league's matchups run from 1 to {final}")
    else:
        key = key or DEFAULT_WINDOW
        if key == "playoffs" and final <= regular:
            raise WindowError("LEAGUE_VALUES_NO_PLAYOFFS", "This league has no playoff matchups")
        lo, hi = {
            "this_week": (current, current),
            "next_4": (current, current + 3),
            "to_playoffs": (current, regular),
            "playoffs": (regular + 1, final),
            "rest_of_season": (current, final),
        }.get(key, (None, None))
        if lo is None:
            raise WindowError("LEAGUE_VALUES_BAD_WINDOW", f"Unknown window {key!r}")
        hi = min(hi, final)
    if hi < current or current > final:
        raise WindowError("LEAGUE_VALUES_WINDOW_OVER", "That window is already over")
    return max(lo, current), hi


def window_days(periods: Mapping[int, Sequence[int]], weeks: Sequence[lp.DatedWeek], first: int, last: int,
                today: date) -> tuple[list[int], list[tuple[date, frozenset[str]]]]:
    """The calendar weeks of matchups `first` to `last`, and their days from today on, each
    with the NBA teams that play it. A week the calendar doesn't carry has no days."""
    numbers: list[int] = []
    for m in range(first, last + 1):
        for w in lp.matchup_weeks(periods, m):
            if w not in numbers:
                numbers.append(w)
    by_number = {w.number: w for w in weeks}
    days = [(day, teams) for n in numbers if n in by_number
            for day, teams in zip(by_number[n].dates(), by_number[n].days) if day >= today]
    return numbers, days


def expected_games(p: lp.ProjPlayer, days: Sequence[tuple[date, frozenset[str]]], today: date,
                   left: Optional[Mapping[str, float]], rostered: bool) -> tuple[int, float]:
    """His NBA team's games left in `days`, and the games he's expected to play in them.
    A game under way today counts only for a rostered player not on his bench or IR (the
    lineup locked him in at the tip), for the share of it still to play."""
    team_games, games = 0, 0.0
    for day, teams in days:
        if p.team is None or p.team not in teams:
            continue
        share = 1.0
        if left is not None and day == today and p.team in left:
            if not rostered or p.slot in NON_STARTING_SLOTS:
                continue
            share = min(max(left[p.team], 0.0), 1.0)
            if share <= 0:
                continue
        team_games += 1
        games += p.chance_on(day) * share
    return team_games, games


def _r(x: Optional[float], places: int = lp.DECIMALS) -> Optional[float]:
    return round(float(x), places) if x is not None else None


def rank_all(values: Sequence[float]) -> list[int]:
    """Each value's rank among all of them, 1 = most; equal values share the better rank."""
    desc = sorted((-v for v in values))
    return [1 + bisect_left(desc, -v) for v in values]


@dataclass(frozen=True)
class Price:
    """One player priced over the window and the rest of the season."""

    per_game: float
    team_games: int
    games: float
    value: float
    season_games: float
    season_value: float
    started: Optional[float]                # in his own roster's lineup; None for a free agent
    window_rank: int
    season_rank: int


def price_league(league: lp.League, free_agents: Sequence[lp.ProjPlayer], weeks: Sequence[lp.DatedWeek],
                 today: date, first: int, last: int, key: Optional[str] = None) -> dict:
    """Every rostered player and each of `free_agents` priced over matchups `first` to
    `last` (from `window_range`) and the rest of the season, with the ranks and tiers
    (see the module docstring). Rostered prices come back per roster in roster order,
    free agents in the order given."""
    slots = active_slots(league.roster_slots)
    team_ids = [r.league_team_id for r in league.rosters]
    regular = league.regular_season_matchups
    final = final_matchup(regular, league.playoff_teams, len(team_ids))
    left = league.today_left
    window_weeks, days = window_days(league.periods, weeks, first, last, today)
    _, season = window_days(league.periods, weeks, league.current_matchup, final, today)
    incomplete = lp.incomplete_weeks(weeks)

    # every player's numbers before the ranks: (player, rostered, started)
    raw: list[tuple[lp.ProjPlayer, bool, Optional[float]]] = []
    for r in league.rosters:
        seated = [0.0] * len(r.players)
        for day, teams in days:
            if teams:
                for i, v in enumerate(lp.day_seats(r.players, teams, day, slots, left if day == today else None)):
                    seated[i] += v
        raw.extend((p, True, s) for p, s in zip(r.players, seated))
    raw.extend((p, False, None) for p in free_agents)

    numbers = []
    for p, rostered, started in raw:
        team_games, games = expected_games(p, days, today, left, rostered)
        _, season_games = expected_games(p, season, today, left, rostered)
        numbers.append((p, team_games, games, season_games, started))
    window_values = [_r(p.value * g) for p, _, g, _, _ in numbers]
    season_values = [_r(p.value * sg) for p, _, _, sg, _ in numbers]
    window_ranks, season_ranks = rank_all(window_values), rank_all(season_values)
    prices = [
        Price(per_game=_r(p.value), team_games=team_games, games=_r(games, 2), value=window_values[i],
              season_games=_r(season_games, 2), season_value=season_values[i],
              started=_r(started) if started is not None else None,
              window_rank=window_ranks[i], season_rank=season_ranks[i])
        for i, (p, team_games, games, season_games, started) in enumerate(numbers)
    ]

    rostered_prices: dict[int, list[Price]] = {}
    at = 0
    for r in league.rosters:
        rostered_prices[r.league_team_id] = prices[at:at + len(r.players)]
        at += len(r.players)

    return {
        "window": {
            "key": key,
            "first_matchup": first,
            "last_matchup": last,
            "weeks": window_weeks,
            "start": days[0][0].isoformat() if days else None,
            "end": days[-1][0].isoformat() if days else None,
            "days": len(days),
            "playoffs": last > regular,
            "schedule_incomplete": any(w in incomplete for w in window_weeks),
            "incomplete_weeks": [w for w in window_weeks if w in incomplete],
        },
        "final_matchup": final,
        "season_end": season[-1][0].isoformat() if season else None,
        "rostered": rostered_prices,
        "free_agents": prices[at:],
        "tiers": tiers({tid: [x.value for x in ps] for tid, ps in rostered_prices.items()}, league.mine),
    }


def tiers(values: Mapping[int, Sequence[float]], mine: Optional[int]) -> list[dict]:
    """Each roster's Nth-best window value against the league's Nth best, N = 1 down to the
    deepest roster: where `mine` ranks, the league's average and median, best and worst."""
    by_team = {tid: sorted(vs, reverse=True) for tid, vs in values.items()}
    depth = max((len(vs) for vs in by_team.values()), default=0)
    out = []
    for k in range(depth):
        at_k = {tid: vs[k] for tid, vs in by_team.items() if len(vs) > k}
        ordered = sorted(at_k.values(), reverse=True)
        you = at_k.get(mine) if mine is not None else None
        out.append({
            "tier": k + 1,
            "you": _r(you),
            "rank": (1 + sum(1 for v in ordered if v > you)) if you is not None else None,
            "average": _r(mean(ordered)),
            "median": _r(median(ordered)),
            "best": _r(ordered[0]),
            "worst": _r(ordered[-1]),
            "teams": len(ordered),
        })
    return out


def positions(slots: frozenset[str]) -> list[str]:
    """The positions a player is listed at, off the lineup slots he can fill."""
    return [s for s in POSITIONS if s in slots]
