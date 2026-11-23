"""
A fantasy league as its managers read it (the Dime autopilot plan, step 2): the
standings and the playoff picture, every matchup on the schedule with its
result, and any team's roster, from one ESPN league read; and what the league's
managers have been adding and dropping, read off the move ledger.

`parse_league` is pure: ESPN's league payload in (mTeam for the teams, their
records and transaction counters; mSettings for the schedule's shape and the
playoff field; mSchedule for every matchup; mStandings for the seeds;
mRoster only when rosters are asked for), plain rows out. `read_league` makes the one read around it, and
values rostered players the way every Court Vision surface does (the league's
own scoring, from our stored stats).

League activity makes no ESPN call at all. usr.move_outcomes already holds
every team's adds and drops, read off the nightly roster snapshots
(services/move_ledger.py), so `read_activity` only regroups them: who moved,
how often, at which positions, and whether the player is still there. It runs
through the league's last stored day, which is last night once the nightly
capture has run; today's moves arrive with tonight's.

How another manager made a move is not ours to show. A rival may use Court
Vision too, and the ledger knows when one of their moves was a stream lane's or
their own Dime's (with Dime's reason); to everyone else in the league it is a
move made outside Court Vision, which is all ESPN shows them as well.

ESPN only, like the snapshots and the ledger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Optional

from core.errors import AppError, BadRequestError
from core.logging import get_logger
from db.base import run_db
from schemas.common import FantasyProvider, LeagueInfo
from services.scoring.providers.espn_settings import ESPN_SCORING_TYPE_MAP
from utils.espn_helpers import POSITION_MAP

log = get_logger("league_overview")

# mStandings rides along for the records and seeds, as in EspnService.get_team_data
LEAGUE_VIEWS = ("mTeam", "mSettings", "mSchedule", "mStandings")
ROSTER_VIEW = "mRoster"
POSITIONS = ("PG", "SG", "SF", "PF", "C")
SEEDING = {"H2H_RECORD": "record", "TOTAL_POINTS_SCORED": "points_for"}

PeriodDates = Callable[[int], Optional[tuple[date, date]]]


class LeagueReadError(AppError):
    """The league can't be read the way these tools need (not an ESPN league, say)."""


# ------------------------------- small readers ------------------------------- #


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def team_name(team: Mapping[str, Any]) -> str:
    """ESPN's `name`, or the older location + nickname pair."""
    name = (team.get("name") or "").strip()
    if not name:
        name = f"{team.get('location') or ''} {team.get('nickname') or ''}".strip()
    return name or f"Team {team.get('id')}"


def _overall(team: Mapping[str, Any]) -> Mapping[str, Any]:
    return ((team.get("record") or {}).get("overall") or {})


def _streak(overall: Mapping[str, Any]) -> Optional[str]:
    length = _int(overall.get("streakLength"))
    kind = {"WIN": "W", "LOSS": "L", "TIE": "T"}.get(str(overall.get("streakType") or "").upper())
    return f"{kind}{length}" if kind and length else None


def games_between(ahead: Mapping[str, Any], behind: Mapping[str, Any]) -> float:
    """How many games `ahead` leads `behind` by: half the win gap plus half the loss gap
    (a tie is half a game either way). Negative when it trails."""
    return ((ahead["wins"] - behind["wins"]) + (behind["losses"] - ahead["losses"])) / 2


# ------------------------------- the standings ------------------------------- #


def standings(payload: Mapping[str, Any], mine: Optional[int], *, categories: bool) -> list[dict[str, Any]]:
    """Every team's record, best first: ESPN's own playoff seed (which applies the league's
    tiebreakers and seeding rule), and for a team with none yet, win percentage then
    points for. Category leagues count categories won, lost and tied."""
    current = _int((payload.get("status") or {}).get("currentMatchupPeriod"))
    rows: list[dict[str, Any]] = []
    for team in payload.get("teams") or []:
        tid = _int(team.get("id"))
        if tid is None:
            continue
        overall = _overall(team)
        wins, losses, ties = (_int(overall.get(k)) or 0 for k in ("wins", "losses", "ties"))
        played = wins + losses + ties
        counter = team.get("transactionCounter") or {}
        totals = counter.get("matchupAcquisitionTotals") or {}
        row: dict[str, Any] = {
            "league_team_id": tid,
            "team_name": team_name(team),
            "mine": tid == mine,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_pct": round((wins + ties / 2) / played, 3) if played else None,
            "streak": _streak(overall),
            "adds_this_matchup": _int(totals.get(str(current))) or 0 if current else None,
            "adds_season": _int(counter.get("acquisitions")) or 0,
            "waiver_rank": _int(team.get("waiverRank")),
            "_seed": _int(team.get("playoffSeed")) or 0,
        }
        if not categories:
            row["points_for"] = round(_num(overall.get("pointsFor")) or 0.0, 1)
            row["points_against"] = round(_num(overall.get("pointsAgainst")) or 0.0, 1)
        rows.append(row)

    rows.sort(key=lambda r: (
        0 if r["_seed"] > 0 else 1,
        r["_seed"] if r["_seed"] > 0 else 0,
        -(r["win_pct"] or 0.0),
        -(r.get("points_for") or 0.0),
        r["league_team_id"],
    ))
    leader = rows[0] if rows else None
    for rank, row in enumerate(rows, start=1):
        del row["_seed"]
        row["rank"] = rank
        row["games_back"] = games_between(leader, row) if leader is not None else 0.0
    return rows


def playoff_picture(rows: list[dict[str, Any]], playoff_teams: Optional[int], *, categories: bool) -> dict[str, Any]:
    """Who holds a playoff spot on today's standings, and by how much. A team in a spot
    carries how far it leads the first team out; a team outside it, how far it trails the
    last team in (games, and in a points league points for as well)."""
    picture: dict[str, Any] = {"playoff_teams": playoff_teams}
    if not playoff_teams or playoff_teams <= 0 or not rows:
        return picture
    if playoff_teams >= len(rows):
        for row in rows:
            row["playoff_spot"] = True
        picture["note"] = "Every team in the league makes the playoffs"
        return picture
    last_in, first_out = rows[playoff_teams - 1], rows[playoff_teams]
    picture["last_in"] = last_in["team_name"]
    picture["first_out"] = first_out["team_name"]
    for i, row in enumerate(rows):
        inside = i < playoff_teams
        row["playoff_spot"] = inside
        if inside:
            row["games_ahead_of_first_out"] = games_between(row, first_out)
            if not categories:
                row["points_ahead_of_first_out"] = round(row["points_for"] - first_out["points_for"], 1)
        else:
            row["games_behind_last_in"] = games_between(last_in, row)
            if not categories:
                row["points_behind_last_in"] = round(last_in["points_for"] - row["points_for"], 1)
    return picture


# ------------------------------- the schedule ------------------------------- #


def _side_score(side: Mapping[str, Any], *, categories: bool, live: bool) -> Optional[float]:
    """A matchup side's score: points, or categories won in a category league."""
    if categories:
        cumulative = side.get("cumulativeScoreLive" if live else "cumulativeScore") or side.get("cumulativeScore")
        if not isinstance(cumulative, Mapping):
            return None
        return _num(cumulative.get("wins"))
    value = side.get("totalPointsLive") if live else None
    if value is None:
        value = side.get("totalPoints")
    number = _num(value)
    return round(number, 1) if number is not None else None


def _dates(period_dates: PeriodDates, matchup: int) -> tuple[Optional[str], Optional[str]]:
    try:
        span = period_dates(matchup)
    except Exception:  # no calendar for the season: the matchup still has its teams
        span = None
    return (span[0].isoformat(), span[1].isoformat()) if span else (None, None)


@dataclass(frozen=True)
class Matchup:
    matchup: int
    home: Optional[int]
    away: Optional[int]
    home_score: Optional[float]
    away_score: Optional[float]
    winner: Optional[str]           # home | away | tie; None while undecided
    bracket: Optional[str]          # None in the regular season; winners | consolation


def matchups(payload: Mapping[str, Any], *, categories: bool) -> list[Matchup]:
    current = _int((payload.get("status") or {}).get("currentMatchupPeriod"))
    out: list[Matchup] = []
    for m in payload.get("schedule") or []:
        period = _int(m.get("matchupPeriodId"))
        if period is None:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        tier = str(m.get("playoffTierType") or "NONE").upper()
        live = period == current
        out.append(Matchup(
            matchup=period,
            home=_int(home.get("teamId")),
            away=_int(away.get("teamId")),
            home_score=_side_score(home, categories=categories, live=live) if home else None,
            away_score=_side_score(away, categories=categories, live=live) if away else None,
            winner={"HOME": "home", "AWAY": "away", "TIE": "tie"}.get(str(m.get("winner") or "").upper()),
            bracket=None if tier == "NONE" else ("winners" if tier.startswith("WINNERS_BRACKET") else "consolation"),
        ))
    out.sort(key=lambda x: (x.matchup, x.home or 0))
    return out


def team_schedule(games: Iterable[Matchup], me: Optional[int], current: Optional[int], names: Mapping[int, str],
                  ranks: Mapping[int, int], period_dates: PeriodDates) -> list[dict[str, Any]]:
    """One team's matchups, first to last: the opponent (with his rank today), and for a
    finished one the result and both scores."""
    rows: list[dict[str, Any]] = []
    if me is None:
        return rows
    for g in games:
        if me not in (g.home, g.away):
            continue
        at_home = g.home == me
        opp = g.away if at_home else g.home
        start, end = _dates(period_dates, g.matchup)
        status = "upcoming" if current is None or g.matchup > current else ("current" if g.matchup == current else "final")
        row: dict[str, Any] = {
            "matchup": g.matchup,
            "start": start,
            "end": end,
            "status": status,
            "opponent": names.get(opp) if opp is not None else None,
            "opponent_league_team_id": opp,
            "opponent_rank": ranks.get(opp) if opp is not None else None,
        }
        if g.bracket:
            row["playoffs"] = g.bracket
        if status != "upcoming":
            row["score"] = g.home_score if at_home else g.away_score
            row["opponent_score"] = g.away_score if at_home else g.home_score
        if status == "final" and g.winner:
            row["result"] = "T" if g.winner == "tie" else ("W" if (g.winner == "home") == at_home else "L")
        rows.append(row)
    return rows


def scoreboard(games: Iterable[Matchup], current: Optional[int], names: Mapping[int, str],
               ranks: Mapping[int, int]) -> list[dict[str, Any]]:
    """Every pairing in the current matchup, with both scores as ESPN reports them."""
    rows = []
    for g in games:
        if current is None or g.matchup != current:
            continue
        rows.append({
            "home": names.get(g.home) if g.home is not None else None,
            "home_rank": ranks.get(g.home) if g.home is not None else None,
            "home_score": g.home_score,
            "away": names.get(g.away) if g.away is not None else None,
            "away_rank": ranks.get(g.away) if g.away is not None else None,
            "away_score": g.away_score,
            **({"playoffs": g.bracket} if g.bracket else {}),
        })
    return rows


# ------------------------------- the whole read ------------------------------- #


def parse_league(payload: Mapping[str, Any], mine: Optional[int], *, categories: bool,
                 period_dates: PeriodDates) -> dict[str, Any]:
    """The league read as Dime's get_league returns it, rosters aside."""
    settings = payload.get("settings") or {}
    sched = settings.get("scheduleSettings") or {}
    status = payload.get("status") or {}
    current = _int(status.get("currentMatchupPeriod"))
    regular = _int(sched.get("matchupPeriodCount"))
    playoff_teams = _int(sched.get("playoffTeamCount"))

    rows = standings(payload, mine, categories=categories)
    picture = playoff_picture(rows, playoff_teams, categories=categories)
    names = {r["league_team_id"]: r["team_name"] for r in rows}
    ranks = {r["league_team_id"]: r["rank"] for r in rows}
    games = matchups(payload, categories=categories)

    season: dict[str, Any] = {
        "current_matchup": current,
        "regular_season_matchups": regular,
        "in_playoffs": bool(current and regular and current > regular),
    }
    if current and regular:
        season["regular_season_matchups_left"] = max(0, regular - current)
    if current:
        start, end = _dates(period_dates, current)
        season["current_start"], season["current_end"] = start, end
    rule = sched.get("playoffSeedingRule")
    if rule:
        picture["seeded_by"] = SEEDING.get(str(rule), str(rule).lower())

    return {
        "league_name": settings.get("name"),
        "teams": len(rows),
        "format": "categories" if categories else "points",
        "season": season,
        "standings": rows,
        "playoffs": picture,
        "scoreboard": scoreboard(games, current, names, ranks),
        "my_schedule": team_schedule(games, mine, current, names, ranks, period_dates),
    }


def league_format(payload: Mapping[str, Any]) -> str:
    """points | categories | roto, from the league's own scoring settings (never a team's
    preview: standings are the league's)."""
    raw = ((payload.get("settings") or {}).get("scoringSettings") or {}).get("scoringType") or "H2H_POINTS"
    return ESPN_SCORING_TYPE_MAP.get(str(raw), ("points", None))[0]


def roster_rows(payload: Mapping[str, Any], league_team_id: int) -> tuple[str, list[dict[str, Any]]]:
    """One team's roster as ESPN has it now: (team name, rows with ESPN ids under `_espn`)."""
    from services.lineup_read_service import parse_espn_lineup

    try:
        parsed = parse_espn_lineup(dict(payload), team_name="", espn_team_id=league_team_id)
    except BadRequestError:
        raise LeagueReadError("LEAGUE_TEAM_NOT_FOUND", f"No team {league_team_id} in this league: "
                                                       "use a league_team_id from the standings")
    rows = []
    for e in sorted(parsed.entries, key=lambda e: (e.lineup_slot_id, e.name)):
        rows.append({
            "_espn": e.player_id,
            "name": e.name,
            "team": e.pro_team,
            "positions": "/".join(POSITION_MAP[s] for s in sorted(e.eligible_slot_ids) if POSITION_MAP.get(s) in POSITIONS),
            "slot": POSITION_MAP.get(e.lineup_slot_id, str(e.lineup_slot_id)),
            "injury": e.injury_status if e.injury_status and e.injury_status != "ACTIVE" else ("injured" if e.injured else None),
        })
    return parsed.team_name, rows


MAX_ROSTERS = 20


async def read_league(league_info: LeagueInfo, *, roster_ids: Iterable[int] = (), all_rosters: bool = False,
                      day: Optional[date] = None, today: Optional[date] = None) -> dict[str, Any]:
    """The league for one of the user's ESPN teams: standings, playoff picture, scoreboard,
    the team's schedule, and the rosters asked for (as ESPN has them now, or as they stood
    on a finished `day`, from the stored snapshots)."""
    from services import schedule_service
    from services.espn_service import EspnService
    from services.lineup_snapshot_service import LineupSnapshotService
    from services.matchup_days import fantasy_today
    from services.player_value_service import PlayerValueService
    from services.providers.identity import nba_ids_by_espn_id

    if league_info.provider != FantasyProvider.ESPN:
        raise LeagueReadError("LEAGUE_ESPN_ONLY", "League standings and rosters are read for ESPN leagues only, "
                                                  "and this is a Yahoo team")
    today = today or fantasy_today()
    if day is not None and day >= today:
        raise LeagueReadError("LEAGUE_DAY_NOT_PAST", f"{day} is not a finished day: leave date out for today's rosters")
    wanted = list(dict.fromkeys(int(i) for i in roster_ids))
    live_rosters = (bool(wanted) or all_rosters) and day is None
    views = [*LEAGUE_VIEWS, *([ROSTER_VIEW] if live_rosters else [])]
    payload = await EspnService.fetch_league(league_info, views)

    me_team = EspnService.resolve_team(league_info, payload.get("teams") or [])
    mine = _int(me_team.get("id")) if me_team else None
    fmt = league_format(payload)
    status = payload.get("status") or {}
    current = _int(status.get("currentMatchupPeriod"))
    latest = _int(status.get("latestScoringPeriod"))
    periods = ((payload.get("settings") or {}).get("scheduleSettings") or {}).get("matchupPeriods") or {}

    def period_dates(matchup: int) -> Optional[tuple[date, date]]:
        # ESPN's own day only corrects the current matchup; any other one is its own weeks
        return schedule_service.get_espn_matchup_dates(periods, matchup, latest if matchup == current else None)

    out = parse_league(payload, mine, categories=fmt != "points", period_dates=period_dates)
    if fmt == "roto":
        out["format"] = "roto"
    out["today"] = today.isoformat()
    if mine is None:
        out["note"] = "Court Vision could not tell which team in this league is the user's"

    names = {r["league_team_id"]: r["team_name"] for r in out["standings"]}
    ids = list(names) if all_rosters else wanted
    unknown = [i for i in ids if i not in names]
    if unknown:
        raise LeagueReadError("LEAGUE_TEAM_NOT_FOUND", f"No team {unknown[0]} in this league: use a "
                                                       "league_team_id from the standings")
    if len(ids) > MAX_ROSTERS:
        ids = ids[:MAX_ROSTERS]
    if not ids:
        return out

    if day is None:
        parsed = {tid: roster_rows(payload, tid) for tid in ids}
        espn_ids = sorted({p["_espn"] for _, rows in parsed.values() for p in rows})
        scoring = await run_db("league.scoring", PlayerValueService.scoring_for, league_info)
        nba_ids = await run_db("league.nba_ids", nba_ids_by_espn_id, espn_ids)
        values = await run_db("league.values", EspnService._values_for, scoring, espn_ids)
        rosters = []
        for tid in ids:
            name, rows = parsed[tid]
            players = []
            for p in rows:
                espn = p.pop("_espn")
                valued = values.get(espn)
                players.append({"player_id": nba_ids.get(espn), **p,
                                "avg_value": round(valued.value, 1) if valued and valued.value is not None else None})
            rosters.append({"league_team_id": tid, "team_name": names.get(tid, name), "mine": tid == mine,
                            "as_of": "now", "players": players})
        out["rosters"] = rosters
        out["value_kind"] = PlayerValueService.value_kind_for(scoring)
        return out

    sides = {tid: names[tid] for tid in ids}
    found = (await LineupSnapshotService.rosters_for_matchup_days(league_info, sides, [day])).get(day, {})
    rosters = []
    for tid in ids:
        snap = found.get(tid)
        if snap is None:
            continue
        rosters.append({
            "league_team_id": tid, "team_name": names[tid], "mine": tid == mine, "as_of": day.isoformat(),
            "players": [
                {"player_id": p.nba_player_id, "name": p.name, "team": p.team, "position": p.position,
                 "slot": p.lineup_slot, "injury": p.injury_status, "points": p.applied_total}
                for p in snap.players
            ],
        })
    out["rosters"] = rosters
    missing = [names[tid] for tid in ids if tid not in found]
    if missing:
        out["rosters_missing"] = missing
    return out


# ------------------------------- league activity (no ESPN call) ------------------------------- #


@dataclass(frozen=True)
class ActivityQuery:
    team_id: int
    provider: str
    provider_league_id: str
    season: int
    provider_team_id: Optional[int]     # the asking team's id in the league
    since: date
    league_team_id: Optional[int] = None
    nba_player_id: Optional[int] = None
    limit: int = 25


def is_mine(row: Any, team_id: int, provider_team_id: Optional[int]) -> bool:
    return row.team_id_id == team_id or (provider_team_id is not None and row.provider_team_id == provider_team_id)


def _player(nba_id: Optional[int], name: Optional[str], positions: Mapping[int, str]) -> Optional[dict[str, Any]]:
    if nba_id is None and name is None:
        return None
    return {"player_id": nba_id, "name": name, "position": positions.get(nba_id) if nba_id is not None else None}


Through = Mapping[int, date]       # each team's last stored day


def _held(row: Any, through: Through) -> Optional[bool]:
    """Whether the added player is still on the team on its last stored day."""
    if row.add_name is None and row.add_nba_id is None:
        return None
    held = (row.windows or {}).get("held_through")
    last = through.get(row.provider_team_id)
    if held is None or last is None:
        return None
    return held >= last.isoformat()


def summarize_activity(rows: list[Any], q: ActivityQuery, through: Through,
                       positions: Mapping[int, str]) -> list[dict[str, Any]]:
    """Per team over the window: adds, drops, how many of the adds are still there, the
    positions it added, and its latest move. Busiest first."""
    teams: dict[int, dict[str, Any]] = {}
    for r in rows:
        t = teams.setdefault(r.provider_team_id, {
            "league_team_id": r.provider_team_id, "team_name": r.team_name,
            "mine": is_mine(r, q.team_id, q.provider_team_id),
            "adds": 0, "drops": 0, "adds_still_held": 0, "_positions": defaultdict(int), "last_move": None,
        })
        day = r.nba_date.isoformat() if hasattr(r.nba_date, "isoformat") else str(r.nba_date)
        if t["last_move"] is None or day > t["last_move"]:
            t["last_move"] = day
            t["team_name"] = r.team_name
        if r.add_name is not None or r.add_nba_id is not None:
            t["adds"] += 1
            if _held(r, through):
                t["adds_still_held"] += 1
            pos = positions.get(r.add_nba_id) if r.add_nba_id is not None else None
            if pos:
                t["_positions"][pos] += 1
        if r.drop_name is not None or r.drop_nba_id is not None:
            t["drops"] += 1
    out = []
    for t in teams.values():
        counts = t.pop("_positions")
        t["positions_added"] = ", ".join(f"{p} {n}" for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        out.append(t)
    out.sort(key=lambda t: (-t["adds"], -t["drops"], t["team_name"]))
    return out


def activity_view(row: Any, q: ActivityQuery, through: Through, positions: Mapping[int, str]) -> dict[str, Any]:
    mine = is_mine(row, q.team_id, q.provider_team_id)
    view: dict[str, Any] = {
        "date": row.nba_date.isoformat() if hasattr(row.nba_date, "isoformat") else str(row.nba_date),
        "team_name": row.team_name,
        "league_team_id": row.provider_team_id,
        "mine": mine,
        "add": _player(row.add_nba_id, row.add_name, positions),
        "drop": _player(row.drop_nba_id, row.drop_name, positions),
    }
    held = _held(row, through)
    if held is not None:
        view["still_held"] = held
        if not held:
            view["held_through"] = (row.windows or {}).get("held_through")
    if mine:
        view["source"] = row.source
    return view


def read_activity(q: ActivityQuery) -> dict[str, Any]:
    """The league's adds and drops since `q.since`, newest first (up to `q.limit`), with a
    per-team summary over all of them. Sync: call through run_db."""
    from peewee import fn

    from db.models.lineup_snapshots import LineupSnapshot
    from db.models.move_outcomes import MoveOutcome
    from db.models.nba.players import Player

    league = (
        (MoveOutcome.provider == q.provider)
        & (MoveOutcome.provider_league_id == q.provider_league_id)
        & (MoveOutcome.season == q.season)
    )
    where = league & (MoveOutcome.nba_date >= q.since)
    if q.league_team_id is not None:
        where &= MoveOutcome.provider_team_id == q.league_team_id
    if q.nba_player_id is not None:
        where &= (MoveOutcome.add_nba_id == q.nba_player_id) | (MoveOutcome.drop_nba_id == q.nba_player_id)
    rows = list(MoveOutcome.select().where(where).order_by(MoveOutcome.nba_date.desc(), MoveOutcome.id.desc()))

    through: dict[int, date] = dict(
        LineupSnapshot.select(LineupSnapshot.provider_team_id, fn.MAX(LineupSnapshot.nba_date))
        .where((LineupSnapshot.provider == q.provider)
               & (LineupSnapshot.provider_league_id == q.provider_league_id)
               & (LineupSnapshot.season == q.season))
        .group_by(LineupSnapshot.provider_team_id)
        .tuples()
    )
    last = max(through.values(), default=None)
    ids = sorted({i for r in rows for i in (r.add_nba_id, r.drop_nba_id) if i is not None})
    positions: dict[int, str] = {}
    if ids:
        positions = {pid: pos for pid, pos in Player.select(Player.id, Player.position)
                     .where(Player.id.in_(ids)).tuples() if pos}
    return {
        "since": q.since.isoformat(),
        "through": last.isoformat() if last is not None else None,
        "total": len(rows),
        "by_team": summarize_activity(rows, q, through, positions),
        "moves": [activity_view(r, q, through, positions) for r in rows[: q.limit]],
    }
