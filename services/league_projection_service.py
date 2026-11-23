"""
League projections for one owned team (the Analysis desk plan, step 1): one ESPN
league read with every roster, the morning's live projection, the season calendar,
then `services.league_projection.project` on the CPU pool.

The read is the same one Dime's get_league makes with all rosters. Each rostered
player is valued under the league's own point weights: his live-projection line per
game and his chance to play each game when the morning's snapshot carries him, else
his stored-stats value with a full chance (none when ESPN lists him OUT), and he is
counted in `players_unprojected`.

The current matchup starts from the live score when today's games can be read
(`GamesService`, the same live overlay the schedule shows): ESPN's settled score
(`totalPoints`, through yesterday) plus tonight's points so far off our own box scores
under the league's weights, the players in a starting slot whose game has started
(`tonight_scored`, as the Week desk's live score counts them). Each NBA team whose game
has started goes to the projection with the share of it still to play (`today_left`),
so tonight counts once. ESPN's own `totalPointsLive` isn't in this read. Without
today's games, or without box scores for games under way, it starts from the settled
score and projects today whole.

ESPN points leagues only for now: a category league needs per-category projections
(the plan's step 6), and Yahoo has no standings read yet. Neither is an error of the
server, so both answer 400 with a code the desk can show.
"""

from __future__ import annotations

import re
import zlib
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

import pytz

from core.compute import run_cpu
from core.errors import BadRequestError, ProviderError, ServiceUnavailableError
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import FantasyProvider, LeagueInfo
from services import league_overview, live_projection, schedule_service
from services import league_projection as lp
from services.draft_congestion import NON_STARTING_SLOTS, week_from_calendar
from utils.espn_helpers import POSITION_MAP

log = get_logger("league_projection")

# ESPN's `playoffSeedingRule` is the tiebreaker between equal records, not the seeding
TIEBREAKERS = {"H2H_RECORD": "head_to_head", "TOTAL_POINTS_SCORED": "points_for"}

EASTERN = pytz.timezone("US/Eastern")
REGULATION_SECONDS = 48 * 60
QUARTER_SECONDS = 12 * 60
# a game under way with no clock to read is guessed from its tip at this length
GAME_LENGTH = timedelta(hours=2, minutes=15)
_CLOCK = re.compile(r"^PT(\d+)M(\d+(?:\.\d+)?)S$")


class LeagueProjectionUnavailable(BadRequestError):
    """The league can't be projected yet (not ESPN, not points)."""


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def roster_slots(payload: Mapping[str, Any]) -> dict[str, int]:
    """The league's lineup slots by ESPN name, bench and IR included."""
    counts = (((payload.get("settings") or {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {})
    out: dict[str, int] = {}
    for slot_id, count in counts.items():
        name = POSITION_MAP.get(_int(slot_id)) if _int(slot_id) is not None else None
        n = _int(count) or 0
        if name and n > 0:
            out[name] = out.get(name, 0) + n
    return out


def matchup_periods(payload: Mapping[str, Any]) -> dict[int, tuple[int, ...]]:
    raw = (((payload.get("settings") or {}).get("scheduleSettings") or {}).get("matchupPeriods") or {})
    out: dict[int, tuple[int, ...]] = {}
    for k, v in raw.items():
        m = _int(k)
        if m is None or not isinstance(v, (list, tuple)):
            continue
        weeks = tuple(w for w in (_int(x) for x in v) if w is not None)
        if weeks:
            out[m] = weeks
    return out


def dated_calendar() -> Optional[tuple[lp.DatedWeek, ...]]:
    """The season's calendar weeks with their first day; None when the season has no
    calendar file (which is not a season with no games left)."""
    try:
        return tuple(
            lp.DatedWeek.of(w["start_date"], week_from_calendar(w["matchup_number"], w["game_span"], w["games"]))
            for w in schedule_service.iter_weeks()
        )
    except FileNotFoundError:
        return None


def settled_scores(payload: Mapping[str, Any]) -> dict[int, float]:
    """Each team's current-matchup score as ESPN settled it overnight (`totalPoints`,
    through yesterday). Tonight's points come from our box scores (`tonight_scored`)."""
    current = _int((payload.get("status") or {}).get("currentMatchupPeriod"))
    out: dict[int, float] = {}
    for m in payload.get("schedule") or []:
        if current is None or _int(m.get("matchupPeriodId")) != current:
            continue
        for side in (m.get("home") or {}, m.get("away") or {}):
            tid, points = _int(side.get("teamId")), side.get("totalPoints")
            if tid is not None and isinstance(points, (int, float)):
                out[tid] = round(float(points), 1)
    return out


def tonight_scored(rosters: list, league_info: LeagueInfo, today: date,
                   started: frozenset[str]) -> Optional[dict[int, float]]:
    """Each league team's points tonight so far, off our live box scores under the
    league's weights: its players in a starting slot whose game has started, as the Week
    desk's live score counts them (`MatchupService.get_live_matchup_by_team_id`). None
    when a rostered player's game has started (`started`, NBA teams) and the box scores
    hold nobody on any roster: the live pipeline isn't writing, and tonight would read
    as nothing scored. Sync: runs through run_db."""
    from db.models.nba.live_player_stats import LivePlayerStats
    from services.matchup_days import score_stat_row
    from services.player_value_service import PlayerValueService

    espn_ids = sorted({e.player_id for _, _, entries in rosters for e in entries})
    if not espn_ids:
        return {tid: 0.0 for tid, _, _ in rosters}
    rows = {r.player.espn_id: r for r in LivePlayerStats.get_live_stats_by_espn_ids(espn_ids, today)}
    if not rows and any(e.pro_team in started for _, _, entries in rosters for e in entries):
        return None
    scoring = PlayerValueService.scoring_for(league_info.model_copy(update={"scoring_preview": None}))
    out: dict[int, float] = {}
    for tid, _name, entries in rosters:
        total = 0.0
        for e in entries:
            row = rows.get(e.player_id)
            if row is None or (row.game_status or 1) < 2:
                continue
            if POSITION_MAP.get(e.lineup_slot_id) in NON_STARTING_SLOTS:
                continue
            total += score_stat_row(row, scoring)
        out[tid] = round(total, 1)
    return out


def with_live_scores(pairings: list, live: Mapping[int, float], current: int) -> Optional[list]:
    """The pairings with the current matchup's live scores; None unless every side has one,
    so no team is read live while another is read settled."""
    out = []
    for p in pairings:
        if p.matchup == current:
            home, away = live.get(p.home), live.get(p.away) if p.away is not None else None
            if home is None or (p.away is not None and away is None):
                return None
            p = lp.Pairing(matchup=p.matchup, home=p.home, away=p.away, home_score=home,
                           away_score=away, winner=p.winner)
        out.append(p)
    return out


def game_left(status: Optional[str], period: Optional[int], clock: Optional[str],
              tip: Optional[datetime], now: datetime) -> Optional[float]:
    """The share of a game still to play: 1 before the tip, 0 once final, and under way
    the regulation time left on its period and clock (overtime counts as almost none).
    A game the live read hasn't caught up with is guessed from its tip time, so one long
    over never counts again. None when nothing tells: not final, not under way, and no
    tip time (the schedule file's games, which a live score can't be read beside)."""
    if status == "final":
        return 0.0
    if status == "in_progress":
        match = _CLOCK.match(clock) if isinstance(clock, str) else None
        if period and match:
            seconds = max(0, 4 - int(period)) * QUARTER_SECONDS + int(match[1]) * 60 + float(match[2])
            return min(max(seconds / REGULATION_SECONDS, 0.0), 1.0)
    if tip is None:
        return 0.5 if status == "in_progress" else None
    if tip > now:
        return 1.0 if status != "in_progress" else 0.5
    return min(max(1.0 - (now - tip) / GAME_LENGTH, 0.0), 1.0)


def today_left(games: Iterable[Any], now: datetime) -> Optional[dict[str, float]]:
    """Each NBA team whose game today has started -> the share of it still to play
    (`game_left`; 1 at the opening tip), off the day's games as
    `GamesService.get_games_on_date` reads them (ET tip times). A team still to tip is
    left out. None when any game's state can't be told, so the live score is never read
    beside a guess that could count tonight twice."""
    out: dict[str, float] = {}
    for g in games:
        tip = None
        if g.start_time_et:
            try:
                day = date.fromisoformat(g.game_date)
                hour, minute = (int(x) for x in g.start_time_et.split(":")[:2])
                tip = EASTERN.localize(datetime(day.year, day.month, day.day, hour, minute))
            except ValueError:
                tip = None
        share = game_left(g.status, g.period, g.game_clock, tip, now)
        if share is None:
            return None
        if g.status not in ("in_progress", "final") and (tip is None or tip > now):
            continue                        # still to tip: its players can still be moved
        for team in (g.home_team, g.away_team):
            if team:
                out[team] = min(out.get(team, 1.0), share)
    return out


async def games_left_today(today: date, now: Optional[datetime] = None) -> Optional[dict[str, float]]:
    """`today_left` for the fantasy day; None when the day's games can't be read."""
    from schemas.common import ApiStatus
    from services.games_service import GamesService

    resp = await GamesService.get_games_on_date(today)
    if resp.status != ApiStatus.SUCCESS or resp.data is None:
        log.warning("league_projection_no_games_today", day=today.isoformat())
        return None
    left = today_left(resp.data.games, (now or datetime.now(EASTERN)).astimezone(EASTERN))
    if left is None:
        log.warning("league_projection_games_state_unknown", day=today.isoformat())
    return left


def parse_inputs(payload: Mapping[str, Any], mine: Optional[int]) -> tuple[list, list, list]:
    """(standings, pairings, rosters as (league team id, name, entries)) off one league read."""
    from services.lineup_read_service import parse_espn_lineup

    rows = league_overview.standings(payload, mine, categories=False)
    current = _int((payload.get("status") or {}).get("currentMatchupPeriod"))
    settled = settled_scores(payload)
    standings = [lp.Standing(
        league_team_id=r["league_team_id"], team_name=r["team_name"], rank=r["rank"],
        wins=r["wins"], losses=r["losses"], ties=r["ties"],
        points_for=r.get("points_for") or 0.0, points_against=r.get("points_against") or 0.0,
    ) for r in rows]
    pairings = []
    for g in league_overview.matchups(payload, categories=False):
        if g.home is None:
            continue
        home, away = g.home_score, g.away_score
        if g.matchup == current:
            # the settled score: `with_live_scores` adds tonight's beside today's games
            home, away = settled.get(g.home), settled.get(g.away) if g.away is not None else None
        pairings.append(lp.Pairing(matchup=g.matchup, home=g.home, away=g.away, home_score=home,
                                   away_score=away, winner=g.winner))
    rosters = []
    for r in rows:
        parsed = parse_espn_lineup(dict(payload), team_name="", espn_team_id=r["league_team_id"])
        rosters.append((r["league_team_id"], r["team_name"], parsed.entries))
    return standings, pairings, rosters


def value_players(rosters: list, league_info: LeagueInfo, today: date):
    """Every rostered player as a ProjPlayer. Sync: runs through run_db."""
    from services.espn_service import EspnService
    from services.player_value_service import PlayerValueService
    from services.providers.identity import nba_ids_by_espn_id

    # the league's own points, never the team's display preview: a points team shown as
    # categories would otherwise value its stored-stats players in category units
    scoring = PlayerValueService.scoring_for(league_info.model_copy(update={"scoring_preview": None}))
    espn_ids = sorted({e.player_id for _, _, entries in rosters for e in entries})
    nba_ids = nba_ids_by_espn_id(espn_ids)
    snapshot = live_projection.load(settings.nba_season, today)
    live = snapshot.players if snapshot is not None else {}
    missing = [e for e in espn_ids if nba_ids.get(e) not in live]
    stored = EspnService._values_for(scoring, missing) if missing else {}

    out = []
    for tid, name, entries in rosters:
        players = []
        for e in entries:
            slots = frozenset(POSITION_MAP[s] for s in e.eligible_slot_ids if POSITION_MAP.get(s))
            nba = nba_ids.get(e.player_id)
            row = live.get(nba) if nba is not None else None
            espn_team = e.pro_team if e.pro_team and e.pro_team != "FA" else None
            slot = POSITION_MAP.get(e.lineup_slot_id)
            if row is not None:
                players.append(lp.ProjPlayer(
                    name=e.name, team=row.team or espn_team, value=max(scoring.points.score(row.line), 0.0),
                    slots=slots, timeline=row.timeline, player_id=nba, projected=True, slot=slot,
                ))
            else:
                valued = stored.get(e.player_id)
                value = valued.value if valued is not None and valued.value is not None else 0.0
                out_now = (e.injury_status or "").upper() == "OUT"
                players.append(lp.ProjPlayer(
                    name=e.name, team=espn_team, value=max(value, 0.0), slots=slots,
                    chance=0.0 if out_now else 1.0, player_id=nba, projected=False, slot=slot,
                ))
        out.append(lp.Roster(league_team_id=tid, team_name=name, players=tuple(players)))
    return tuple(out), snapshot


def notes_for(result: Mapping[str, Any], snapshot_as_of: Optional[date], mine: Optional[int],
              divisions: int = 1, tiebreaker: Optional[str] = None) -> list[str]:
    notes = []
    gaps = [w for w in result["incomplete_weeks"]
            if any(w in m["weeks"] and m["status"] != "final" for m in result["matchups"])]
    if gaps:
        weeks = " and ".join(str(w) for w in gaps) if len(gaps) <= 2 else ", ".join(str(w) for w in gaps)
        notes.append(f"The calendar is missing games in week{'s' if len(gaps) > 1 else ''} {weeks} that the NBA "
                     "hasn't scheduled yet, so every roster is undercounted there; the weekly averages leave "
                     "them out.")
    if snapshot_as_of is None:
        notes.append("No live projection this morning: players are valued off stored stats and expected to "
                     "play every game.")
    unprojected = sum(len(t["players_unprojected"]) for t in result["teams"])
    if snapshot_as_of is not None and unprojected:
        one = unprojected == 1
        notes.append(f"{unprojected} rostered player{'' if one else 's'} had no live projection and "
                     f"{'is' if one else 'are'} valued off stored stats.")
    if mine is None:
        notes.append("Court Vision could not tell which team in this league is yours.")
    if tiebreaker == "head_to_head" and not result["in_playoffs"]:
        notes.append("This league breaks tied records head to head; the odds break them by points for.")
    if divisions > 1:
        notes.append(f"This league has {divisions} divisions, and the odds treat it as one table, so a division "
                     "winner's place in the playoffs isn't modeled.")
    if result["in_playoffs"]:
        notes.append("The playoffs have started: odds are no longer simulated.")
    return notes


async def league_projection(league_info: LeagueInfo, *, today: Optional[date] = None,
                            runs: int = lp.SIMULATIONS) -> dict[str, Any]:
    """The league projected for one of the user's ESPN teams (see the module docstring)."""
    from services.espn_service import EspnService
    from services.matchup_days import fantasy_today

    if league_info.provider != FantasyProvider.ESPN:
        raise LeagueProjectionUnavailable(
            "LEAGUE_PROJECTION_ESPN_ONLY", "League projections are for ESPN leagues for now")
    today = today or fantasy_today()
    payload = await EspnService.fetch_league(
        league_info, [*league_overview.LEAGUE_VIEWS, league_overview.ROSTER_VIEW])
    fmt = league_overview.league_format(payload)
    if fmt != "points":
        raise LeagueProjectionUnavailable(
            "LEAGUE_PROJECTION_POINTS_ONLY", "League projections are for points leagues for now; "
                                             "category leagues come later")

    me = EspnService.resolve_team(league_info, payload.get("teams") or [])
    mine = _int(me.get("id")) if me else None
    sched = ((payload.get("settings") or {}).get("scheduleSettings") or {})
    status = payload.get("status") or {}
    current = _int(status.get("currentMatchupPeriod")) or 1
    regular = _int(sched.get("matchupPeriodCount")) or current
    playoff_teams = _int(sched.get("playoffTeamCount")) or 0

    rule = sched.get("playoffSeedingRule")
    tiebreaker = TIEBREAKERS.get(str(rule), str(rule).lower()) if rule else None
    standings, pairings, entries = parse_inputs(payload, mine)
    if any(p.matchup == current and (p.home_score is None or (p.away is not None and p.away_score is None))
           for p in pairings):
        # a side without its score so far would skew the matchup and the odds, so no projection
        raise ProviderError("espn", "ESPN didn't send this matchup's scores so far; try again in a minute",
                            error_code="LEAGUE_PROJECTION_NO_SETTLED_SCORE")
    rosters, snapshot = await run_db("league_projection.values", value_players, entries, league_info, today)
    weeks = await run_db("league_projection.calendar", dated_calendar)
    if weeks is None:
        raise ServiceUnavailableError("LEAGUE_PROJECTION_NO_CALENDAR",
                                      "The season calendar isn't loaded, so the league can't be projected")
    # tonight's points as scored, and today only what's left of each game; else the settled
    # score with today whole
    left = await games_left_today(today)
    tonight = None
    if left is not None:
        tonight = await run_db("league_projection.tonight", tonight_scored, entries, league_info, today,
                               frozenset(left))
    live = None
    if tonight is not None:
        settled = {s: v for p in pairings if p.matchup == current
                   for s, v in ((p.home, p.home_score), (p.away, p.away_score)) if s is not None}
        live = with_live_scores(pairings, {t: round(v + tonight.get(t, 0.0), 1) for t, v in settled.items()
                                           if v is not None}, current)
    if live is not None:
        pairings = live
    else:
        left = None
    league = lp.League(
        rosters=rosters, standings=tuple(standings), schedule=tuple(pairings),
        periods=matchup_periods(payload), current_matchup=current, regular_season_matchups=regular,
        playoff_teams=playoff_teams, roster_slots=roster_slots(payload),
        mine=mine, reseed=bool(sched.get("playoffReseed")), today_left=left,
    )
    # the same runs for the same league on the same day, so a reload doesn't move the odds
    seed = zlib.crc32(f"{league_info.league_id}:{league_info.year}:{today.isoformat()}".encode())
    result = await run_cpu("league_projection.project", lp.project, league, weeks, today, runs=runs, seed=seed)

    log.info("league_projection_built", league_id=league_info.league_id, teams=len(rosters),
             runs=result["simulations"], live_as_of=str(snapshot.as_of) if snapshot else None,
             live_score=left is not None)
    return {
        "league_name": (payload.get("settings") or {}).get("name"),
        "format": "points",
        "today": today.isoformat(),
        "projection_as_of": snapshot.as_of.isoformat() if snapshot else None,
        "teams_count": len(rosters),
        "playoff_teams": playoff_teams,
        "seed_tiebreaker": tiebreaker,
        "lineup_slots": {k: v for k, v in league.roster_slots.items() if k not in ("BE", "IR")},
        **result,
        "notes": notes_for(result, snapshot.as_of if snapshot else None, mine,
                           divisions=len(sched.get("divisions") or []) or 1, tiebreaker=tiebreaker),
    }
