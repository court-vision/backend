"""
The streaming line for one owned team (the Analysis desk plan, step 3): your players'
points per game against the free agents optimal streaming picks up, and the spots it
streams, matchup by matchup over a window (`services.streaming_line`).

The reads are value over a window's (`services.league_values_service`): one ESPN league
read with every roster, ESPN's 150 most-owned free agents and waiver claims, the
morning's live projection. Only your roster is priced. Beside them: the days and who
plays them from the games table (with each day's first tip-off, so the current matchup
plans from tomorrow once today's games have started), your protected players, the
pickups you already have scheduled, and the league's adds (`services.acquisitions`, off
the same read). Each matchup gets its own adds: the current one what's left of it less
the pickups scheduled in it, a later one its whole allowance, and none more than the
season cap leaves.

Each matchup's plans take up to a second, so a window is at most `MAX_MATCHUPS` long;
the rest of the season needs an overnight run. ESPN points leagues only, as the league
projection: the same 400 codes for anything else.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from core.compute import run_cpu
from core.errors import BadRequestError, ServiceUnavailableError
from core.logging import get_logger
from db.base import run_db
from schemas.common import LeagueInfo
from services import league_projection as lp
from services import league_projection_service as lps
from services import league_values as lv
from services import league_values_service as lvs
from services import streaming_line as sl
from services.lineup_planner import ACTIVE_SLOT_IDS, IR_SLOT_ID

log = get_logger("streaming_line")

MAX_MATCHUPS = 4


def slot_counts(payload: dict) -> dict[int, int]:
    """The league's active lineup slots by ESPN id (bench and IR left out)."""
    counts = (((payload.get("settings") or {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {})
    out: dict[int, int] = {}
    for slot_id, count in counts.items():
        sid, n = lps._int(slot_id), lps._int(count) or 0
        if sid is not None and sid in ACTIVE_SLOT_IDS and n > 0:
            out[sid] = n
    return out


def matchup_dates(periods, weeks, matchup: int) -> Optional[tuple[date, date]]:
    """A matchup's first and last day off the calendar's weeks; None when it carries none."""
    by_number = {w.number: w for w in weeks}
    dated = [by_number[n] for n in lp.matchup_weeks(periods, matchup) if n in by_number]
    if not dated:
        return None
    return min(w.start for w in dated), max(w.end for w in dated)


def matchup_adds(acq, matchup: int, current: int, game_days: Optional[int], pending_in: int,
                 season_left: Optional[int]) -> Optional[int]:
    """The adds the lanes may plan with in `matchup`: its allowance (what's left of it, for the
    current one) less the pickups already scheduled in it, and no more than the season cap leaves.
    None: no limit, or the league's isn't known."""
    from services.acquisitions import with_game_days

    if acq is None:
        return None
    rooms = []
    sized = with_game_days(acq, game_days) if game_days is not None else acq
    if sized.limit is not None:
        used = acq.matchup_used if matchup == current else 0
        rooms.append(sized.limit - used - pending_in)
    if season_left is not None:
        rooms.append(season_left)
    return max(0, min(rooms)) if rooms else None


def free_agents_for(pool: list) -> list[sl.LineFreeAgent]:
    """The free agents a lane could take, as the lanes read the pool: out players left out, a
    waiver claim available the day after his waivers clear."""
    from services.lineup_planner import OUT_STATUSES
    from services.stream_lane_service import active_slots

    out = []
    for f, p in pool:
        if (f.injury_status or "ACTIVE").upper() in OUT_STATUSES:
            continue
        available_on = None
        if f.acquisition_status == "waivers":
            if f.waivers_until is None:
                continue
            available_on = f.waivers_until + timedelta(days=1)
        out.append(sl.LineFreeAgent(f.player_id, p, active_slots(f.valid_positions), available_on))
    return out


def window_rate(rates: list[tuple[Optional[float], float]]) -> Optional[float]:
    """Per-game lines over a window: each matchup's weighted by its pickups' games."""
    games = sum(g for r, g in rates if r is not None)
    if games <= 0:
        return None
    return round(sum(r * g for r, g in rates if r is not None) / games, sl.DECIMALS)


def window_band(weights: list[tuple[Optional[float], float, Optional[float], float]]
                ) -> tuple[Optional[float], Optional[float]]:
    """A window's line and its low end from each matchup's (line, games, low, low games). Each
    matchup's low is at most its line, but the two averages weigh different games, so they can
    cross: the low end is held at the line."""
    line = window_rate([(r, g) for r, g, _, _ in weights])
    low = window_rate([(r, g) for _, _, r, g in weights])
    return line, (min(low, line) if low is not None and line is not None else low)


def notes_for(snapshot_as_of, listed, acq, season_left, asked_adds, incomplete, unplanned) -> list[str]:
    notes = []
    if incomplete:
        weeks = " and ".join(str(w) for w in incomplete)
        notes.append(f"The calendar is missing games in week{'s' if len(incomplete) > 1 else ''} {weeks} that the "
                     "NBA hasn't scheduled yet, so streams there are undercounted.")
    if snapshot_as_of is None:
        notes.append("No live projection this morning: players are valued off stored stats and expected to "
                     "play every game.")
    if listed is None:
        notes.append("ESPN's free agent list didn't load, so no lane has anyone to add and there is no line.")
    if acq is None:
        notes.append(f"Court Vision couldn't read this league's add limits, so up to {sl.MAX_LANES} lanes are "
                     "tried with no limit.")
    elif season_left is not None and asked_adds > season_left:
        notes.append(f"You have {season_left} adds left this season, fewer than these matchups allow together; "
                     "each matchup is worked out with up to that many on its own.")
    if unplanned:
        notes.append("No day is left in " + " and ".join(f"matchup {m}" for m in unplanned)
                     + " that a pickup could count for.")
    notes.append("The line assumes today's free agents stay free and play as projected, so it runs a little high: "
                 "line_low is the line with one add fewer and the best streamer taken.")
    return notes


async def streaming_line(team_id: int, league_info: LeagueInfo, *, window: Optional[str] = None,
                         first_matchup: Optional[int] = None, last_matchup: Optional[int] = None,
                         today: Optional[date] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Your players against the streaming line over a window of matchups (see the module
    docstring)."""
    from services import roster_protection
    from services.acquisitions import matchup_game_days, parse_acquisitions, season_room
    from services.espn_service import EspnService
    from services.matchup_days import fantasy_today
    from services.stream_lane_service import (
        _days,
        _pending_pickups,
        first_open_period,
        rehold_after_for,
    )

    lps.ensure_espn(league_info)
    payload, listed = await asyncio.gather(lps.read_points_league(league_info), lvs.read_free_agents(league_info))
    today = today or fantasy_today()
    now = now or datetime.now(timezone.utc)

    teams = payload.get("teams") or []
    me = EspnService.resolve_team(league_info, teams)
    mine = lps._int(me.get("id")) if me else None
    if mine is None:
        raise BadRequestError("LEAGUE_STREAMING_LINE_NO_TEAM",
                              "Court Vision could not tell which team in this league is yours")
    sched = ((payload.get("settings") or {}).get("scheduleSettings") or {})
    current = lps._int((payload.get("status") or {}).get("currentMatchupPeriod")) or 1
    regular = lps._int(sched.get("matchupPeriodCount")) or current
    playoff_teams = lps._int(sched.get("playoffTeamCount")) or 0
    _standings, _pairings, entries = lps.parse_inputs(payload, mine)
    final = lv.final_matchup(regular, playoff_teams, len(entries))
    try:
        first, last = lv.window_range(window, first_matchup, last_matchup, current=current, regular=regular,
                                      final=final)
    except lv.WindowError as e:
        raise BadRequestError(e.code, e.message) from None
    if last - first + 1 > MAX_MATCHUPS:
        raise BadRequestError("LEAGUE_STREAMING_LINE_TOO_LONG",
                              f"The streaming line is worked out for up to {MAX_MATCHUPS} matchups at a time")
    key = (window or lv.DEFAULT_WINDOW) if first_matchup is None and last_matchup is None else None

    own = [(tid, name, es) for tid, name, es in entries if tid == mine]
    if not own:
        raise BadRequestError("LEAGUE_STREAMING_LINE_NO_TEAM", "Your roster isn't in this league's read")
    rosters, snapshot = await run_db("streaming_line.roster", lps.value_players, own, league_info, today)
    rostered = {e.player_id for _, _, es in entries for e in es}
    pool = await run_db("streaming_line.free_agents", lvs.value_free_agents, listed or [], league_info, snapshot,
                        rostered)
    weeks = await run_db("streaming_line.calendar", lps.dated_calendar)
    if weeks is None:
        raise ServiceUnavailableError("LEAGUE_PROJECTION_NO_CALENDAR",
                                      "The season calendar isn't loaded, so the line can't be worked out")
    periods = lps.matchup_periods(payload)
    spans = {m: matchup_dates(periods, weeks, m) for m in range(first, last + 1)}
    dated = [s for s in spans.values() if s is not None]
    if not dated:
        raise ServiceUnavailableError("LEAGUE_PROJECTION_NO_CALENDAR", "The calendar has none of these matchups")
    span_end = max(e for _, e in dated)
    lane_days = await run_db("streaming_line.days", _days, 0, max((span_end - today).days, 0), 0, today)
    pending = await run_db("streaming_line.pending", _pending_pickups, team_id)
    protected = await roster_protection.ids_soft(team_id)

    acq = parse_acquisitions(payload, me)
    season_left = season_room(acq) - len(pending) if acq is not None and season_room(acq) is not None else None
    if season_left is not None:
        season_left = max(season_left, 0)
    rehold_after = rehold_after_for(acq)
    open_from = first_open_period(lane_days[0], now) if lane_days else 0
    slots = slot_counts(payload)

    entries_mine = own[0][2]
    players = [sl.LinePlayer(e.player_id, p, frozenset(s for s in e.eligible_slot_ids if s in ACTIVE_SLOT_IDS),
                             protected=e.player_id in protected, on_ir=e.lineup_slot_id == IR_SLOT_ID)
               for e, p in zip(entries_mine, rosters[0].players, strict=True)]
    free_agents = free_agents_for(pool) if listed is not None else []

    matchups, unplanned, asked_adds = [], [], 0
    worth_by: dict[int, list[float]] = {p.espn_id: [] for p in players}
    weights: list[tuple[Optional[float], float, Optional[float], float]] = []
    streamed_in: dict[int, list[int]] = {}
    for m in range(first, last + 1):
        span = spans.get(m)
        days = [] if span is None else [
            (d.nba_date, d.teams) for d in lane_days
            if span[0] <= d.nba_date <= span[1] and (d.nba_date > today or d.period >= open_from)]
        game_days = await matchup_game_days(league_info.year, span[0], span[1]) if span else None
        if span is not None and game_days is None:
            game_days = sum(1 for d in lane_days if span[0] <= d.nba_date <= span[1] and d.teams) or None
        pending_in = sum(1 for pk in pending if span is not None and span[0] <= pk.nba_date <= span[1])
        adds = matchup_adds(acq, m, current, game_days, pending_in, season_left)
        asked_adds += adds or 0
        if not days:
            unplanned.append(m)
        result = await run_cpu("streaming_line.matchup", sl.matchup_line, players, free_agents, days, slots, adds,
                               rehold_after=rehold_after)
        for p in players:
            worth_by[p.espn_id].append(result.worth.get(p.espn_id, 0.0))
        by_id = {p.espn_id: p.proj.name for p in players}
        matchups.append({
            "matchup": m,
            "start": days[0][0].isoformat() if days else None,
            "end": days[-1][0].isoformat() if days else None,
            "days": result.days,
            "adds": result.adds,
            "playoffs": m > regular,
            "base": result.base,
            "lanes": [{"lane": ln.lane, "holder": by_id.get(ln.holder, str(ln.holder)), "holder_espn_id": ln.holder,
                       "total": ln.total, "gain": ln.gain, "adds": ln.adds} for ln in result.lanes],
            "worth_running": result.worth_running,
            "line": result.line,
            "line_low": result.line_low,
            "games": round(result.games, sl.DECIMALS),
            "streamed": [{"name": by_id.get(i, str(i)), "espn_id": i} for i in result.streamed],
            "proven": all(ln.optimal for ln in result.lanes),
        })
        weights.append((result.line, result.games, result.line_low, result.low_games))
        streamed_in.update({i: streamed_in.get(i, []) + [m] for i in result.streamed})

    line, line_low = window_band(weights)
    rows = []
    for e, p in zip(entries_mine, players, strict=True):
        worth = round(sum(worth_by[p.espn_id]), sl.DECIMALS)
        per_game = round(max(p.proj.value, 0.0), sl.DECIMALS)
        rows.append({
            "name": p.proj.name, "espn_id": p.espn_id, "player_id": p.proj.player_id, "nba_team": p.proj.team,
            "positions": lv.positions(p.proj.slots), "slot": p.proj.slot,
            "injury_status": e.injury_status if e.injury_status != "ACTIVE" else None,
            "protected": p.protected, "on_ir": p.on_ir, "per_game": per_game,
            "below_line": (per_game < line) if line is not None and not p.on_ir else None,
            "streamed_in": streamed_in.get(p.espn_id, []),
            "worth": worth, "by_matchup": worth_by[p.espn_id],
        })
    rows.sort(key=lambda r: (-r["per_game"], r["name"]))

    incomplete = sorted({w for m in range(first, last + 1) for w in lp.matchup_weeks(periods, m)}
                        & set(lp.incomplete_weeks(weeks)))
    log.info("streaming_line_built", league_id=league_info.league_id, window=f"{first}-{last}",
             free_agents=len(free_agents), lanes=[x["worth_running"] for x in matchups], line=line)
    return {
        "league_name": (payload.get("settings") or {}).get("name"),
        "today": today.isoformat(),
        "projection_as_of": snapshot.as_of.isoformat() if snapshot else None,
        "current_matchup": current,
        "key": key,
        "first_matchup": first,
        "last_matchup": last,
        "line": line,
        "line_low": line_low,
        "matchups": matchups,
        "players": rows,
        "free_agents_read": len(pool) if listed is not None else None,
        "notes": notes_for(snapshot.as_of if snapshot else None, listed, acq, season_left, asked_adds, incomplete,
                           unplanned),
    }
