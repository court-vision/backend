"""
League projections: every roster's weeks ahead under the league's own scoring, the
league's projections as the benchmark a team is tested against, and playoff odds
(the Analysis desk plan, step 1).

A team on its own says little: 1,250 points a week is a contender in one league and
a seller in the next. So every roster in the league is projected the same way, and a
team's number is read against the rest: the median, the playoff line (the score of
the last team in, `playoff_teams`-th best) and the best.

**A roster's day.** Each player carries his points per game under the league's
scoring and, from the live projection (`services.live_projection`), the chance he
plays each of his team's games. On every day left, the players whose team plays are
seated in the league's lineup slots best first (`draft_congestion.lineup`, the
matching the draft board already trusts), each at his points times his chance; the
seated values are the roster's day. A day's sum is an expectation, not a lineup: a
manager who sets lineups daily gets close to it. Players on IR are projected like
the rest: the timeline already has them out until their return.

**A week, a matchup.** Calendar weeks are the calendar's (`schedule_service`); a
matchup period is the weeks ESPN maps it to (a two-week playoff round is two).
The current matchup starts from the live score when today's games can be read
(`League.today_left`): what's been played counts as played, and today adds only the
rest of each game, the share of it still to play. A player whose game has started is
held in the slot ESPN has him in (a lineup locks at each game's tip), so the players
still to play are seated in the slots left. Without today's games it starts from
ESPN's settled score (through yesterday) and counts today whole, as projected; either
way a game played today is counted once.

**Weeks the calendar can't count.** The NBA sets some games late (the NBA Cup's
knockout window in December). A week with more empty days than its span explains
is `incomplete`: its rows are flagged, and the per-week averages leave it out,
since every roster is undercounted there alike.

**Playoff odds.** The remaining regular-season matchups are played `SIMULATIONS`
times. Each roster's projection carries a season-long error (`TEAM_SD`) and each
matchup its own noise, whose spread grows with the points still to score
(`WEEK_SD` of a typical week for a typical week). The final records then seed the
playoff field, ties broken by points for (a league that breaks ties head to head
gets points for too: the runs don't track who beat whom), and the bracket is
played over the league's playoff matchups with the same noise, re-paired by seed
each round when the league reseeds. Divisions aren't modeled: the league is one
table. Seeds, playoff and title odds are counts over
the runs. The noise is not yet calibrated against this season's results; the
constants are the prototype's.

Pure: no database, no ESPN, no clock. `services.league_projection_service` reads
the inputs and calls `project`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Mapping, Optional, Sequence

import numpy as np

from services.draft_congestion import NON_STARTING_SLOTS, CongestionPlayer, SampleWeek, active_slots, lineup
from services.live_projection import Run, chance_on

# A full week's score varies by this share of a typical week (sd), the matchup's noise.
WEEK_SD = 0.08
# A roster's projection is off by this share for the whole season (sd), shared by all its weeks.
TEAM_SD = 0.03
SIMULATIONS = 10_000
DECIMALS = 1


# ------------------------------- inputs ------------------------------- #


@dataclass(frozen=True)
class ProjPlayer:
    """One rostered player as the projection sees him."""

    name: str
    team: Optional[str]                     # NBA tricode whose games he plays; None plays none
    value: float                            # points per game he plays, under the league's scoring
    slots: frozenset[str]                   # ESPN lineup slots he can start in
    timeline: tuple[Run, ...] = ()          # chance per game (live projection); empty = `chance`
    chance: float = 1.0                     # chance per game when there is no timeline
    player_id: Optional[int] = None         # NBA id
    projected: bool = True                  # False: valued off stored stats, no live projection
    slot: Optional[str] = None              # the ESPN lineup slot he's in now; None when not known
    unplayed: bool = False                  # True: hasn't played this season, so plays no games

    def chance_on(self, day: date) -> float:
        return chance_on(self.timeline, day) if self.timeline else self.chance


@dataclass(frozen=True)
class Roster:
    league_team_id: int
    team_name: str
    players: tuple[ProjPlayer, ...]


@dataclass(frozen=True)
class Standing:
    """A team's record before the current matchup, as ESPN reports it."""

    league_team_id: int
    team_name: str
    rank: int
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float


@dataclass(frozen=True)
class Pairing:
    """One matchup of the schedule: its teams and, once scored, their scores."""

    matchup: int
    home: int
    away: Optional[int]                     # None: a bye
    home_score: Optional[float] = None
    away_score: Optional[float] = None
    winner: Optional[str] = None            # home | away | tie, once final


@dataclass(frozen=True)
class DatedWeek:
    """One calendar week: its number, first day, and per day the NBA teams that play."""

    number: int
    start: date
    days: tuple[frozenset[str], ...]

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(len(self.days))]

    @property
    def end(self) -> date:
        return self.start + timedelta(days=len(self.days) - 1)

    @classmethod
    def of(cls, start: date, week: SampleWeek) -> "DatedWeek":
        return cls(number=week.number, start=start, days=week.days)


@dataclass(frozen=True)
class League:
    """Everything `project` needs about the league, read once."""

    rosters: tuple[Roster, ...]
    standings: tuple[Standing, ...]
    schedule: tuple[Pairing, ...]
    periods: Mapping[int, tuple[int, ...]]  # matchup period -> calendar weeks
    current_matchup: int
    regular_season_matchups: int
    playoff_teams: int
    roster_slots: Mapping[str, int]         # ESPN slot name -> count, bench and IR included
    mine: Optional[int] = None
    reseed: bool = False                    # survivors re-paired by seed after each round
    # NBA team whose game today has started -> the share of it still to play (1 at the opening
    # tip, 0 once final), read with the live score the current matchup starts from; a team still
    # to tip isn't in it. None: the settled score, today whole
    today_left: Optional[Mapping[str, float]] = None


# ------------------------------- the calendar ------------------------------- #


def incomplete_weeks(weeks: Sequence[DatedWeek]) -> frozenset[int]:
    """Weeks with more days without a game than their span explains: one quiet day a
    week (Thanksgiving, Christmas Eve), and in a longer week its extra days (the
    All-Star break). The Cup's knockout window leaves three to five."""
    out = set()
    for w in weeks:
        empty = sum(1 for teams in w.days if not teams)
        if empty > 1 + max(0, len(w.days) - 7):
            out.add(w.number)
    return frozenset(out)


def matchup_weeks(periods: Mapping[int, Sequence[int]], matchup: int) -> tuple[int, ...]:
    """The calendar weeks a matchup is played over (its own number when ESPN maps none)."""
    weeks = periods.get(matchup)
    return tuple(int(w) for w in weeks) if weeks else (matchup,)


# ------------------------------- a roster's days ------------------------------- #


def _seat(players: Sequence[ProjPlayer], teams: frozenset[str], day: date, slots: Sequence[str],
          left: Optional[Mapping[str, float]]) -> tuple[list[float], float]:
    """Each player's points in the day's lineup, in `players` order (0 for one benched,
    idle or out), and the day's total (see `day_value`)."""
    out = [0.0] * len(players)
    playing: list[CongestionPlayer] = []
    worth: list[float] = []
    index: list[int] = []                   # playing[k] is players[index[k]]
    free = list(slots)
    held = 0.0
    for i, p in enumerate(players):
        if p.team is None or p.team not in teams:
            continue
        if left is not None and p.team in left:     # his game has started: his slot is locked
            share = min(max(left[p.team], 0.0), 1.0)
            if p.slot in NON_STARTING_SLOTS:
                continue
            if p.slot in free:              # held whatever he's worth: nobody else can take it
                free.remove(p.slot)
            out[i] = max(p.value, 0.0) * p.chance_on(day) * share
            held += out[i]
            continue
        if p.value <= 0:
            continue
        ev = p.value * p.chance_on(day)
        if ev <= 0:
            continue
        playing.append(CongestionPlayer(id=len(playing), value=ev, team=p.team, slots=p.slots))
        worth.append(ev)
        index.append(i)
    if not playing:
        return out, held
    if not slots:                           # no lineup known: everyone counts
        for k, i in enumerate(index):
            out[i] = worth[k]
        return out, held + sum(worth)
    _seats, owner = lineup(playing, free)
    for k in owner:
        if k is not None:
            out[index[k]] = worth[k]
    return out, held + sum(worth[k] for k in owner if k is not None)


def day_value(players: Sequence[ProjPlayer], teams: frozenset[str], day: date, slots: Sequence[str],
              left: Optional[Mapping[str, float]] = None) -> float:
    """A roster's expected points on one day: the players whose team plays, seated in
    the lineup slots best first, each worth his points times his chance to play.

    With `left` (today, read live: each NBA team whose game has started, with the share
    of it still to play), a player whose game has started is locked where ESPN has him:
    in a starting slot he holds it and adds the rest of his game, on the bench or IR he
    adds nothing. The players still to play are seated in the slots the locked ones leave."""
    return _seat(players, teams, day, slots, left)[1]


def day_seats(players: Sequence[ProjPlayer], teams: frozenset[str], day: date, slots: Sequence[str],
              left: Optional[Mapping[str, float]] = None) -> list[float]:
    """What each player adds to that day's lineup (`day_value`'s seating), in `players`
    order: his expected points when seated, 0 when benched, idle or out. They sum to
    `day_value`."""
    return _seat(players, teams, day, slots, left)[0]


def week_values(roster: Roster, weeks: Sequence[DatedWeek], first_day: date,
                slots: Sequence[str], today_left: Optional[Mapping[str, float]] = None) -> dict[int, float]:
    """A roster's expected points in each calendar week, counting days from `first_day`
    (that day read with `today_left`, when given)."""
    out: dict[int, float] = {}
    for w in weeks:
        total = 0.0
        for day, teams in zip(w.dates(), w.days):
            if day >= first_day and teams:
                total += day_value(roster.players, teams, day, slots,
                                   today_left if day == first_day else None)
        out[w.number] = total
    return out


# ------------------------------- benchmarks ------------------------------- #


def _r(x: Optional[float]) -> Optional[float]:
    return round(float(x), DECIMALS) if x is not None else None


def benchmark(values: Mapping[int, float], mine: Optional[int], line_rank: int) -> dict:
    """One measure across the league: the median, the playoff line (the `line_rank`-th
    best), the best and worst, the middle half, and where `mine` stands (rank 1 = best)."""
    if not values:
        return {"you": None, "rank": None, "median": None, "low": None, "high": None,
                "line": None, "best": None, "worst": None, "teams": 0}
    ordered = sorted(values.values(), reverse=True)
    arr = np.array(ordered, dtype=float)
    you = values.get(mine) if mine is not None else None
    rank = (1 + sum(1 for v in ordered if v > you)) if you is not None else None
    line = ordered[line_rank - 1] if 0 < line_rank <= len(ordered) else None
    return {
        "you": _r(you), "rank": rank,
        "median": _r(median(ordered)),
        "low": _r(float(np.percentile(arr, 25))), "high": _r(float(np.percentile(arr, 75))),
        "line": _r(line), "best": _r(ordered[0]), "worst": _r(ordered[-1]),
        "teams": len(ordered),
    }


def ranks(values: Mapping[int, float]) -> dict[int, int]:
    """1 = most; ties share the better rank."""
    ordered = sorted(values.values(), reverse=True)
    return {tid: 1 + sum(1 for v in ordered if v > val) for tid, val in values.items()}


# ------------------------------- the noise ------------------------------- #


def score_sd(mean: float, typical_week: float) -> float:
    """The spread of a score still to come: `WEEK_SD` of a typical week for a typical
    week's points, growing with the square root of the points left (a score is a sum
    of many games, so its variance grows with them)."""
    if mean <= 0 or typical_week <= 0:
        return 0.0
    return WEEK_SD * math.sqrt(mean * typical_week)


def win_chance(my_base: float, my_mean: float, their_base: float, their_mean: float,
               typical_week: float) -> float:
    """The chance one side outscores the other: normal over both sides' noise and
    their season-long errors."""
    diff = (my_base + my_mean) - (their_base + their_mean)
    var = (score_sd(my_mean, typical_week) ** 2 + score_sd(their_mean, typical_week) ** 2
           + (TEAM_SD * my_mean) ** 2 + (TEAM_SD * their_mean) ** 2)
    if var <= 0:
        return 1.0 if diff > 0 else (0.0 if diff < 0 else 0.5)
    return 0.5 * (1 + math.erf(diff / math.sqrt(2 * var)))


# ------------------------------- the bracket ------------------------------- #


def bracket_order(size: int) -> list[int]:
    """Seeds in bracket order for a field of `size` (a power of two): 1 meets `size`,
    and the top two seeds can only meet in the final. [1, 8, 4, 5, 2, 7, 3, 6] for 8."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [s for seed in order for s in (seed, n + 1 - seed)]
    return order


def playoff_rounds(playoff_teams: int) -> int:
    return math.ceil(math.log2(playoff_teams)) if playoff_teams > 1 else 0


# ------------------------------- the simulation ------------------------------- #


@dataclass
class Odds:
    playoffs: dict[int, float] = field(default_factory=dict)
    title: dict[int, float] = field(default_factory=dict)
    wins: dict[int, float] = field(default_factory=dict)
    seeds: dict[int, list[float]] = field(default_factory=dict)
    runs: int = 0


def simulate(
    team_ids: Sequence[int],
    standings: Mapping[int, Standing],
    games: Sequence[tuple[int, int, int]],             # (matchup, home, away) left in the regular season
    base: Mapping[tuple[int, int], float],             # (team, matchup) -> points already scored
    mean: Mapping[tuple[int, int], float],             # (team, matchup) -> points still to come
    playoff_matchups: Sequence[int],
    playoff_teams: int,
    typical_week: float,
    runs: int = SIMULATIONS,
    seed: int = 0,
    reseed: bool = False,
) -> Odds:
    """Play the rest of the season `runs` times (see the module docstring). With `reseed`
    the survivors of each round are paired again by seed, best against worst."""
    rng = np.random.default_rng(seed)
    idx = {tid: i for i, tid in enumerate(team_ids)}
    n_teams = len(team_ids)
    odds = Odds(runs=runs)
    if n_teams == 0:
        return odds

    factor = rng.normal(1.0, TEAM_SD, size=(runs, n_teams))
    wins = np.tile(np.array([standings[t].wins + standings[t].ties / 2 for t in team_ids], dtype=float), (runs, 1))
    pf = np.tile(np.array([standings[t].points_for for t in team_ids], dtype=float), (runs, 1))

    def scores(tid: int, matchup: int) -> np.ndarray:
        m = mean.get((tid, matchup), 0.0)
        noise = rng.normal(0.0, score_sd(m, typical_week), size=runs) if m > 0 else 0.0
        return base.get((tid, matchup), 0.0) + m * factor[:, idx[tid]] + noise

    for matchup, home, away in games:
        h, a = scores(home, matchup), scores(away, matchup)
        hi, ai = idx[home], idx[away]
        wins[:, hi] += np.where(h > a, 1.0, np.where(h == a, 0.5, 0.0))
        wins[:, ai] += np.where(a > h, 1.0, np.where(h == a, 0.5, 0.0))
        pf[:, hi] += h
        pf[:, ai] += a

    # seeds: by record, ties broken by points for
    order = np.lexsort((-pf, -wins), axis=1)                    # order[r, k] = team index seeded k+1
    seeds = np.empty_like(order)
    np.put_along_axis(seeds, order, np.arange(n_teams)[None, :].repeat(runs, axis=0), axis=1)

    field_size = min(max(playoff_teams, 0), n_teams)
    for tid in team_ids:
        i = idx[tid]
        odds.wins[tid] = float(wins[:, i].mean())
        odds.seeds[tid] = [float((seeds[:, i] == k).mean()) for k in range(n_teams)]
        odds.playoffs[tid] = float((seeds[:, i] < field_size).mean()) if field_size else 0.0

    rounds = playoff_rounds(field_size)
    if rounds == 0 or len(playoff_matchups) < rounds:
        for tid in team_ids:
            odds.title[tid] = odds.playoffs[tid] if field_size == 1 else 0.0
        return odds

    size = 2 ** rounds
    slots = np.full((runs, size), -1, dtype=int)
    for pos, s in enumerate(bracket_order(size)):
        if s <= field_size:
            slots[:, pos] = order[:, s - 1]
    round_means = np.array([[mean.get((tid, m), 0.0) for tid in team_ids] for m in playoff_matchups[:rounds]])
    for r in range(rounds):
        mu = round_means[r]
        a, b = slots[:, 0::2], slots[:, 1::2]
        sa = np.where(a >= 0, mu[np.maximum(a, 0)] * np.take_along_axis(factor, np.maximum(a, 0), axis=1), -1.0)
        sb = np.where(b >= 0, mu[np.maximum(b, 0)] * np.take_along_axis(factor, np.maximum(b, 0), axis=1), -1.0)
        sa = sa + rng.standard_normal(sa.shape) * WEEK_SD * np.sqrt(np.maximum(sa, 0) * typical_week)
        sb = sb + rng.standard_normal(sb.shape) * WEEK_SD * np.sqrt(np.maximum(sb, 0) * typical_week)
        slots = np.where(b < 0, a, np.where(a < 0, b, np.where(sa >= sb, a, b)))
        if reseed and slots.shape[1] > 2:
            rank = np.where(slots >= 0, np.take_along_axis(seeds, np.maximum(slots, 0), axis=1), n_teams)
            by_seed = np.take_along_axis(slots, np.argsort(rank, axis=1, kind="stable"), axis=1)
            slots = by_seed[:, [s - 1 for s in bracket_order(slots.shape[1])]]
    champ = slots[:, 0]
    for tid in team_ids:
        odds.title[tid] = float((champ == idx[tid]).mean())
    return odds


# ------------------------------- the whole projection ------------------------------- #


def _score(p: Pairing, tid: int) -> Optional[float]:
    return p.home_score if p.home == tid else p.away_score


def project(league: League, weeks: Sequence[DatedWeek], today: date, *, runs: int = SIMULATIONS,
            seed: int = 0) -> dict:
    """Every roster projected, the benchmarks, the matchups, and the odds (see the module
    docstring). Returns plain rows for the service to wrap."""
    slots = active_slots(league.roster_slots)
    by_number = {w.number: w for w in weeks}
    incomplete = incomplete_weeks(weeks)
    team_ids = [r.league_team_id for r in league.rosters]
    names = {r.league_team_id: r.team_name for r in league.rosters}
    standings = {s.league_team_id: s for s in league.standings}
    mine = league.mine
    line_rank = max(1, min(league.playoff_teams, len(team_ids))) if league.playoff_teams else 0

    # every roster's weeks from today
    week_points = {r.league_team_id: week_values(r, weeks, today, slots, league.today_left)
                   for r in league.rosters}

    current, regular = league.current_matchup, league.regular_season_matchups
    rounds = playoff_rounds(min(league.playoff_teams, len(team_ids)))
    playoff_matchups = [regular + i for i in range(1, rounds + 1)]
    last = playoff_matchups[-1] if playoff_matchups else regular
    all_matchups = list(range(1, last + 1))

    def projected(tid: int, matchup: int) -> float:
        return sum(week_points[tid].get(w, 0.0) for w in matchup_weeks(league.periods, matchup))

    # the per-week averages: whole weeks still to come, the calendar's gaps left out
    def per_week(matchups: Sequence[int]) -> Optional[dict[int, float]]:
        cal = [w for m in matchups for w in matchup_weeks(league.periods, m)
               if w in by_number and by_number[w].start > today and w not in incomplete]
        if not cal:
            return None
        return {tid: sum(week_points[tid].get(w, 0.0) for w in cal) / len(cal) for tid in team_ids}

    regular_avg = per_week([m for m in all_matchups if current <= m <= regular])
    playoff_avg = per_week(playoff_matchups)
    typical = median(regular_avg.values()) if regular_avg else (
        median(playoff_avg.values()) if playoff_avg else 0.0)

    # the schedule as played and to come
    pairings: dict[tuple[int, int], Pairing] = {}
    for p in league.schedule:
        pairings[(p.matchup, p.home)] = p
        if p.away is not None:
            pairings[(p.matchup, p.away)] = p

    base: dict[tuple[int, int], float] = {}
    mean: dict[tuple[int, int], float] = {}
    for tid in team_ids:
        for m in all_matchups:
            if m < current:
                continue
            mean[(tid, m)] = projected(tid, m)
            if m == current:
                p = pairings.get((m, tid))
                base[(tid, m)] = (_score(p, tid) or 0.0) if p is not None else 0.0

    games = [(p.matchup, p.home, p.away) for p in league.schedule
             if p.away is not None and current <= p.matchup <= regular
             and p.home in standings and p.away in standings]
    in_playoffs = current > regular
    odds = None
    if not in_playoffs and team_ids:
        odds = simulate(team_ids, standings, games, base, mean, playoff_matchups,
                        league.playoff_teams, typical, runs=runs, seed=seed,
                        reseed=league.reseed)

    # one row per matchup: the league's scores (final) or projections (to come)
    matchups = []
    for m in all_matchups:
        cal = matchup_weeks(league.periods, m)
        dated = [by_number[w] for w in cal if w in by_number]
        status = "final" if m < current else ("current" if m == current else "upcoming")
        values: dict[int, float] = {}
        for tid in team_ids:
            if status == "final":
                p = pairings.get((m, tid))
                score = _score(p, tid) if p is not None else None
                if score is not None:
                    values[tid] = score
            else:
                values[tid] = base.get((tid, m), 0.0) + mean.get((tid, m), 0.0)
        row = {
            "matchup": m,
            "weeks": list(cal),
            "start": dated[0].start.isoformat() if dated else None,
            "end": dated[-1].end.isoformat() if dated else None,
            "status": status,
            "playoffs": m > regular,
            "schedule_incomplete": any(w in incomplete for w in cal),
            **benchmark(values, mine, line_rank),
        }
        p = pairings.get((m, mine)) if mine is not None else None
        if p is not None and p.away is not None:
            opp = p.away if p.home == mine else p.home
            row["opponent"] = names.get(opp)
            row["opponent_league_team_id"] = opp
            if status == "final":
                row["opponent_score"] = _r(_score(p, opp))
                if p.winner:
                    row["result"] = "T" if p.winner == "tie" else (
                        "W" if (p.winner == "home") == (p.home == mine) else "L")
            else:
                row["opponent_score"] = _r(values.get(opp))
                row["win_chance"] = round(win_chance(
                    base.get((mine, m), 0.0), mean.get((mine, m), 0.0),
                    base.get((opp, m), 0.0), mean.get((opp, m), 0.0), typical), 3)
        matchups.append(row)

    reg_rank = ranks(regular_avg) if regular_avg else {}
    po_rank = ranks(playoff_avg) if playoff_avg else {}
    teams = []
    for tid in team_ids:
        st = standings.get(tid)
        roster = next(r for r in league.rosters if r.league_team_id == tid)
        teams.append({
            "league_team_id": tid,
            "team_name": names[tid],
            "mine": tid == mine,
            "standing": st.rank if st else None,
            "wins": st.wins if st else 0, "losses": st.losses if st else 0, "ties": st.ties if st else 0,
            "points_for": _r(st.points_for) if st else None,
            "points_against": _r(st.points_against) if st else None,
            "regular_season_week": _r(regular_avg[tid]) if regular_avg else None,
            "regular_season_rank": reg_rank.get(tid),
            "playoff_week": _r(playoff_avg[tid]) if playoff_avg else None,
            "playoff_rank": po_rank.get(tid),
            "projected_wins": round(odds.wins[tid], 1) if odds else None,
            "playoff_odds": round(odds.playoffs[tid], 3) if odds else None,
            "title_odds": round(odds.title[tid], 3) if odds else None,
            "seed_odds": [round(x, 3) for x in odds.seeds[tid]] if odds else [],
            "players_projected": sum(1 for p in roster.players if p.projected),
            "players_unprojected": [p.name for p in roster.players if not p.projected],
        })
    teams.sort(key=lambda t: (t["standing"] or 99, t["league_team_id"]))

    return {
        "current_matchup": current,
        "regular_season_matchups": regular,
        "playoff_matchups": playoff_matchups,
        "playoff_weeks": [w for m in playoff_matchups for w in matchup_weeks(league.periods, m)],
        "incomplete_weeks": sorted(w for w in incomplete if w in {x for m in all_matchups for x in matchup_weeks(league.periods, m)}),
        "typical_week": _r(typical),
        "simulations": odds.runs if odds else 0,
        "in_playoffs": in_playoffs,
        "benchmarks": {
            "regular_season": benchmark(regular_avg or {}, mine, line_rank),
            "playoffs": benchmark(playoff_avg or {}, mine, line_rank),
        },
        "teams": teams,
        "matchups": matchups,
    }
