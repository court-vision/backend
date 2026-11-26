"""
Value over a window for one owned team's league (the Analysis desk plan, step 2):
every rostered player and the league's best free agents priced for a run of its
matchups (`services.league_values`).

The reads are the league projection's (`services.league_projection_service`): one ESPN
league read with every roster, the morning's live projection, the season calendar, and
today's games so far. Beside them, ESPN's free agents and waiver claims for the league,
most-owned first (the Week desk market's read), valued the same way: off the live
projection when the morning's snapshot carries them, else off stored stats with a full
chance (none when ESPN lists them OUT). Every player is valued under the league's own
point weights, never the team's display preview.

A free agent list that doesn't load prices the rosters alone, and says so. ESPN points
leagues only, as the league projection: the same 400 codes for anything else.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Optional

from core.compute import run_cpu
from core.errors import BadRequestError, ProviderError, ServiceUnavailableError
from core.logging import get_logger
from db.base import run_db
from schemas.common import LeagueInfo
from services import league_projection as lp
from services import league_projection_service as lps
from services import league_values as lv

log = get_logger("league_values")

# the free agents read and priced (they count in the ranks), and the most shown by default
FREE_AGENTS_READ = 150
FREE_AGENTS_SHOWN = 50


async def read_free_agents(league_info: LeagueInfo, count: int = FREE_AGENTS_READ) -> Optional[list]:
    """ESPN's `count` most-owned free agents and waiver claims in the league, or None when
    ESPN doesn't answer."""
    from services.espn_service import EspnService

    try:
        resp = await EspnService.get_free_agents(league_info.model_copy(update={"scoring_preview": None}), count)
    except ProviderError as e:
        log.warning("league_values_free_agents_failed", league_id=league_info.league_id,
                    error_code=getattr(e, "error_code", None))
        return None
    return list(resp.data or [])


def value_free_agents(free_agents: list, league_info: LeagueInfo, snapshot, rostered: set[int]) -> list:
    """Each free agent ESPN listed (a rostered one left out) beside his ProjPlayer, valued
    as `league_projection_service.value_players` values a rostered player. Sync: runs
    through run_db."""
    from services.player_value_service import PlayerValueService
    from services.providers.identity import nba_ids_by_espn_id

    listed = [f for f in free_agents if f.player_id not in rostered]
    if not listed:
        return []
    scoring = PlayerValueService.scoring_for(league_info.model_copy(update={"scoring_preview": None}))
    nba_ids = nba_ids_by_espn_id(sorted({f.player_id for f in listed}))
    live = snapshot.players if snapshot is not None else {}
    out = []
    for f in listed:
        slots = frozenset("UT" if s.startswith("UT") else s for s in f.valid_positions)
        team = f.team if f.team and f.team != "FA" else None
        nba = nba_ids.get(f.player_id)
        row = live.get(nba) if nba is not None else None
        if row is not None:
            p = lp.ProjPlayer(name=f.name, team=row.team or team, value=max(scoring.points.score(row.line), 0.0),
                              slots=slots, timeline=row.timeline, player_id=nba, projected=True)
        else:
            out_now = (f.injury_status or "").upper() == "OUT"
            p = lp.ProjPlayer(name=f.name, team=team, value=max(f.avg_points or 0.0, 0.0), slots=slots,
                              chance=0.0 if out_now else 1.0, player_id=nba, projected=False)
        out.append((f, p))
    return out


def _row(p: lp.ProjPlayer, price: lv.Price, **meta: Any) -> dict[str, Any]:
    return {
        "name": p.name, "player_id": p.player_id, "nba_team": p.team, "positions": lv.positions(p.slots),
        "per_game": price.per_game, "team_games": price.team_games, "games": price.games, "value": price.value,
        "started": price.started, "window_rank": price.window_rank,
        "season_games": price.season_games, "season_value": price.season_value, "season_rank": price.season_rank,
        "projected": p.projected, **meta,
    }


def notes_for(result: dict[str, Any], snapshot_as_of: Optional[date], unprojected: int,
              free_agents_read: Optional[int], mine: Optional[int]) -> list[str]:
    notes = []
    gaps = result["window"]["incomplete_weeks"]
    if gaps:
        weeks = " and ".join(str(w) for w in gaps)
        notes.append(f"The calendar is missing games in week{'s' if len(gaps) > 1 else ''} {weeks} that the NBA "
                     "hasn't scheduled yet, so every player is undercounted there alike.")
    if snapshot_as_of is None:
        notes.append("No live projection this morning: players are valued off stored stats and expected to "
                     "play every game.")
    elif unprojected:
        one = unprojected == 1
        notes.append(f"{unprojected} player{'' if one else 's'} had no live projection and "
                     f"{'is' if one else 'are'} valued off stored stats.")
    if free_agents_read is None:
        notes.append("ESPN's free agent list didn't load, so only rostered players are priced and ranked.")
    if mine is None:
        notes.append("Court Vision could not tell which team in this league is yours.")
    return notes


async def league_values(league_info: LeagueInfo, *, window: Optional[str] = None,
                        first_matchup: Optional[int] = None, last_matchup: Optional[int] = None,
                        free_agents: int = FREE_AGENTS_SHOWN, today: Optional[date] = None) -> dict[str, Any]:
    """The league's players priced over a window for one of the user's ESPN teams (see the
    module docstring)."""
    from services.espn_service import EspnService
    from services.matchup_days import fantasy_today

    lps.ensure_espn(league_info)
    payload, listed = await asyncio.gather(lps.read_points_league(league_info), read_free_agents(league_info))
    today = today or fantasy_today()

    me = EspnService.resolve_team(league_info, payload.get("teams") or [])
    mine = lps._int(me.get("id")) if me else None
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
    key = (window or lv.DEFAULT_WINDOW) if first_matchup is None and last_matchup is None else None

    rosters, snapshot = await run_db("league_values.rosters", lps.value_players, entries, league_info, today)
    rostered = {e.player_id for _, _, es in entries for e in es}
    pool = await run_db("league_values.free_agents", value_free_agents, listed or [], league_info, snapshot,
                        rostered)
    weeks = await run_db("league_values.calendar", lps.dated_calendar)
    if weeks is None:
        raise ServiceUnavailableError("LEAGUE_PROJECTION_NO_CALENDAR",
                                      "The season calendar isn't loaded, so the league can't be priced")
    league = lp.League(
        rosters=rosters, standings=(), schedule=(), periods=lps.matchup_periods(payload),
        current_matchup=current, regular_season_matchups=regular, playoff_teams=playoff_teams,
        roster_slots=lps.roster_slots(payload), mine=mine, today_left=await lps.games_left_today(today),
    )
    result = await run_cpu("league_values.price", lv.price_league, league, [p for _, p in pool], weeks, today,
                           first, last, key)

    players = []
    for (tid, team_name, es), roster in zip(entries, rosters):
        for e, p, price in zip(es, roster.players, result["rostered"][tid]):
            players.append(_row(p, price, espn_id=e.player_id, league_team_id=tid, team_name=team_name,
                                mine=tid == mine, slot=p.slot, availability=None,
                                injury_status=e.injury_status if e.injury_status != "ACTIVE" else None))
    free = [_row(p, price, espn_id=f.player_id, league_team_id=None, team_name=None, mine=False, slot=None,
                 availability=f.acquisition_status, injury_status=f.injury_status)
            for (f, p), price in zip(pool, result["free_agents"])]
    free.sort(key=lambda r: (r["window_rank"], r["season_rank"], r["name"]))
    players.extend(free[:max(free_agents, 0)])
    players.sort(key=lambda r: (r["window_rank"], r["season_rank"], r["name"]))

    unprojected = sum(1 for r in rosters for p in r.players if not p.projected) + sum(
        1 for _, p in pool if not p.projected)
    log.info("league_values_built", league_id=league_info.league_id, window=f"{first}-{last}",
             rostered=sum(len(r.players) for r in rosters), free_agents=len(pool),
             live_as_of=str(snapshot.as_of) if snapshot else None,
             today_read_live=league.today_left is not None)
    return {
        "league_name": (payload.get("settings") or {}).get("name"),
        "format": "points",
        "today": today.isoformat(),
        "projection_as_of": snapshot.as_of.isoformat() if snapshot else None,
        "current_matchup": current,
        "regular_season_matchups": regular,
        "final_matchup": result["final_matchup"],
        "season_end": result["season_end"],
        "window": result["window"],
        "players": players,
        "free_agents_read": len(pool) if listed is not None else None,
        "ranked_players": len(result["free_agents"]) + sum(len(r.players) for r in rosters),
        "tiers": result["tiers"],
        "notes": notes_for(result, snapshot.as_of if snapshot else None, unprojected,
                           len(pool) if listed is not None else None, mine),
    }
