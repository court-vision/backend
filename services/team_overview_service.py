"""
Every team in the account in one read (the all-teams home desk, step 1).

    GET /teams/overview  ->  overview(user_id)

For each of the user's teams, side by side:

- the Week desk's daily actions (`DailyActionsService.read`: one board read and the
  free-agent pool), each judged against the team's automation levels (`judge`), which
  gives the team its one state (`state_of`);
- the matchup, read from the provider once and shared by the live score
  (`MatchupService.get_live_matchup_by_team_id`, through services/matchup_hold.py) and
  the week's days (`MatchupService.weekly_of`);
- the league's projection for the record, standing, playoff odds and this matchup's win
  chance (`league_projection`), kept SEASON_TTL and never waited on for long;
- what is scheduled next: the day's auto-lineup, a pending pickup, a lane's next
  decision, Dime's next check-in.

What keeps it honest and cheap:

- **One team never fails the others.** A team whose board can't be read is `cant_read`
  with its own error; any other block that fails is None and named in `missing`.
- **Covered means an automation is set to make the move, and can.** A lineup move is
  covered by auto-lineup only before its window closes on a day it hasn't run, by the
  late-scratch swap or Dime only while they run, and by nothing once the lock is
  ESCALATE_BEFORE_LOCK away or the team can't be written. Otherwise it needs the user.
- **Bounded.** TEAMS_AT_ONCE teams are read at a time, so an account with many teams
  can't take every ESPN slot; league projections run one at a time (the CPU pool has two
  workers) and a read waits SEASON_BUDGET for one before answering without it
  (`season_state: pending`), leaving it to finish for the next read. Each build logs how
  long it and each team took (`teams_overview_built`).
- **Kept briefly.** The whole answer is kept OVERVIEW_TTL per user and each team's week
  WEEK_TTL, both dropped by any provider write this process makes (core/inflight.py's
  write epoch), so a move sent from a desk shows on the next read.

Never writes, and spends nothing on AI.
"""

from __future__ import annotations

import asyncio
import json
import time as clock
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from api.deps import OwnedTeamContext, ensure_team_owned, load_owned_league_info
from core.errors import AppError
from core.inflight import InFlight, write_epoch
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from db.models.nba.games import Game
from db.models.roster_moves import COUNTED_AUTO_STATUSES
from db.models.teams import Team
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.daily_actions import DailyAction, DailyActionsData
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.team_overview import (
    OverviewAutomation,
    OverviewCategories,
    OverviewDay,
    OverviewError,
    OverviewItem,
    OverviewMatchup,
    OverviewNext,
    OverviewSeason,
    OverviewSeat,
    OverviewTonight,
    TeamOverview,
    TeamsOverviewData,
)
from services import matchup_hold
from services.ai import autonomy, checkins, levels
from services.ai.automations import FILL_WINDOW_CLOSES, _pending_pickups, _todays_writes, fill_setting
from services.daily_actions_service import DailyActionsService
from services.league_projection_service import LeagueProjectionUnavailable, league_projection
from services.lineup_planner import IR_SLOT_ID, OUT_STATUSES
from services.matchup_days import BENCH_SLOT_ID, counts_for_the_day
from services.matchup_service import MatchupService
from services.providers import unavailable_message
from services.scheduled_pickup_service import et_at
from services.stream_lane_service import StreamLaneService

log = get_logger("team_overview")

# Teams read side by side. A team is three ESPN reads at once (board, free-agent pool, matchup) and
# the process holds eight in flight for every user, a read failing after five seconds in the queue:
# three teams ask for nine, so one waits a moment and nobody else's desk is starved
TEAMS_AT_ONCE = 3
# An automation that hasn't made its lineup move this close to the lock isn't counted on
ESCALATE_BEFORE_LOCK = timedelta(minutes=10)
# With no box score to say so, a game that tipped this long ago is over
GAME_SURELY_OVER = timedelta(hours=3)
OVERVIEW_TTL = 20.0
WEEK_TTL = 300.0
SEASON_TTL = 1800.0
# A league that can't be projected (its format) is asked again after SEASON_TTL; a failed read sooner
SEASON_RETRY = 120.0
SEASON_BUDGET = 1.5
MAX_KEPT = 512

SCRATCH, START = "scratch", "start"
LINEUP_KINDS = (SCRATCH, START)
# What Court Vision makes of the list at daily_actions `act`: IR moves and open-spot adds, never a drop
ACT_KINDS = ("ir_in", "ir_out", "add")
FAILED_STATUSES = ("rejected", "failed")
MAY_SIT = frozenset({"QUESTIONABLE", "DOUBTFUL", "DAY_TO_DAY", "GTD", "DTD"})
MARKS = ("live", "later", "final", "questionable", "out", "idle", "empty")
STATES = ("needs_you", "your_call", "set", "autopilot", "cant_read")
_STATE_ORDER = {"needs_you": 0, "cant_read": 1, "your_call": 2, "set": 3, "autopilot": 4}
_STANDING_ORDER = {"needs_you": 0, "your_call": 1, "covered": 2}
_FAR = datetime.max.replace(tzinfo=timezone.utc)


# ------------------------------- the rules (pure) ------------------------------- #


@dataclass(frozen=True)
class Rules:
    """What a team's automations will do today, as far as the items' standing depends on it."""

    can_write: bool = False                           # the board can be written at all
    late_scratch: str = autonomy.OFF                  # the levels the list's moves fall under
    daily_actions: str = autonomy.OFF
    fill_applies: bool = False                        # auto-lineup writes this team's fill
    fill_ran: bool = False                            # and has today (applied, or nothing to move)
    fill_closes_at: Optional[datetime] = None         # its window's end; None on a day without games
    dime_acts: frozenset[str] = frozenset()           # the rows Dime sends itself
    next_checkin_at: Optional[datetime] = None        # Dime's next scheduled check-in
    fill_failed: bool = False                         # the fill's write today was refused or failed
    scratch_failed: frozenset[int] = frozenset()      # NBA ids whose late-scratch swap today was, too


def tip_at(nba_date: Optional[str], hhmm: Optional[str]) -> Optional[datetime]:
    """An ET tip-off ("19:30") on the board's day, as UTC."""
    if not nba_date or not hhmm:
        return None
    try:
        hour, minute = (int(x) for x in hhmm.split(":")[:2])
        return et_at(date.fromisoformat(nba_date), time(hour, minute))
    except ValueError:
        return None


def deadline_of(action: DailyAction, state: LineupState) -> Optional[str]:
    """The tip that takes a lineup move away: the earliest among the players it moves (a seat
    locks when its holder's game starts). Other kinds have none."""
    if action.kind not in LINEUP_KINDS:
        return None
    by_id = {p.player_id: p for p in state.players}
    tips = [p.game_time_et for m in action.moves
            if (p := by_id.get(m.player_id)) is not None and p.has_game_today and p.game_time_et]
    if not tips and action.game_time_et:
        tips = [action.game_time_et]
    return min(tips) if tips else None


def judge(action: DailyAction, deadline: Optional[datetime], rules: Rules, now: datetime
          ) -> tuple[str, Optional[str]]:
    """An item's standing and, when covered, the automation set to make it."""
    kind = action.kind
    if kind in LINEUP_KINDS:
        cover: Optional[str] = None
        ruled = action.counterpart.nba_player_id if action.counterpart is not None else None
        if (rules.fill_applies and not rules.fill_ran and not rules.fill_failed
                and rules.fill_closes_at is not None and now < rules.fill_closes_at):
            cover = "auto_lineup"                  # the fill benches a starter we ruled out, too
        elif kind == SCRATCH and autonomy.LATE_SCRATCH in rules.dime_acts:
            cover = "dime"                         # an event check-in before his game locks
        elif kind == SCRATCH and rules.late_scratch == autonomy.ACT and ruled not in rules.scratch_failed:
            cover = "late_scratch"
        elif (kind == START and autonomy.LINEUP in rules.dime_acts and rules.next_checkin_at is not None
              and deadline is not None and rules.next_checkin_at < deadline):
            cover = "dime"
        if cover is not None and deadline is not None and now >= deadline - ESCALATE_BEFORE_LOCK:
            cover = None
        if cover is not None and rules.can_write:
            return "covered", cover
        return "needs_you", None
    if rules.can_write:
        if autonomy.DAILY_ACTIONS in rules.dime_acts:
            return "covered", "dime"
        if rules.daily_actions == autonomy.ACT and kind in ACT_KINDS and action.blocked_reason is None:
            return "covered", "daily_actions"
    return "your_call", None


def state_of(items: Sequence[OverviewItem], *, autopilot: bool) -> str:
    """The team's one state. `autopilot`: the switch is on and Dime's check-ins run."""
    if any(i.standing == "needs_you" for i in items):
        return "needs_you"
    if autopilot:
        return "autopilot"
    if any(i.standing == "your_call" for i in items):
        return "your_call"
    return "set"


def items_of(data: DailyActionsData, rules: Rules, now: datetime) -> list[OverviewItem]:
    """The day's actions with their standing: what needs the user first, soonest lock first."""
    state = data.lineup
    out: list[OverviewItem] = []
    for action in data.actions:
        hhmm = deadline_of(action, state) if state is not None else None
        at = tip_at(data.nba_date, hhmm)
        standing, cover = judge(action, at, rules, now)
        out.append(OverviewItem(standing=standing, covered_by=cover, deadline_et=hhmm if at else None,
                                deadline_at=at, action=action))
    out.sort(key=lambda i: (_STANDING_ORDER[i.standing], i.deadline_at or _FAR))
    return out


# ------------------------------- tonight (pure) ------------------------------- #


def game_states(rosters: Iterable[Any]) -> tuple[dict[int, Any], dict[str, int]]:
    """Tonight's box scores off a live matchup's rosters: each player's own row by ESPN id, and each
    NBA team's game status (1 scheduled, 2 on, 3 final) as its rostered players' rows say."""
    rows: dict[int, Any] = {}
    teams: dict[str, int] = {}
    for p in rosters:
        live = getattr(p, "live", None)
        if live is None:
            continue
        rows[p.player_id] = live
        teams[p.team] = max(teams.get(p.team, 0), int(live.game_status))
    return rows, teams


def mark_of(p: LineupPlayer, status: Optional[int], tip: Optional[datetime], now: datetime) -> str:
    """A player's mark tonight. `status` is his game's (his own box score row, else a teammate's);
    without one a game that has tipped is on until it is surely over."""
    if not p.has_game_today:
        return "idle"
    if (p.injury_status or "ACTIVE").upper() in OUT_STATUSES:
        return "out"
    if status == 3:
        return "final"
    if status == 2:
        return "live"
    if p.game_started:
        return "final" if tip is not None and now - tip >= GAME_SURELY_OVER else "live"
    if (p.injury_status or "ACTIVE").upper() in MAY_SIT:
        return "questionable"
    return "later"


def tonight_of(state: LineupState, rows: Mapping[int, Any], teams: Mapping[str, int], now: datetime, *,
               points_league: bool) -> OverviewTonight:
    """The board as the desk draws it: the starting seats in order (empty ones included), the
    bench and IR, each player's mark, and the next tip that locks anyone."""

    def seat(p: LineupPlayer) -> OverviewSeat:
        live = rows.get(p.player_id)
        status = int(live.game_status) if live is not None else teams.get(p.team)
        mark = mark_of(p, status, tip_at(state.nba_date, p.game_time_et), now)
        scored = live is not None and int(live.game_status) >= 2 and points_league
        return OverviewSeat(
            slot_id=p.lineup_slot_id, slot=p.lineup_slot, mark=mark, player_id=p.player_id,
            nba_player_id=p.nba_player_id, name=p.name, team=p.team, injury_status=p.injury_status,
            opponent=p.opponent, game_time_et=p.game_time_et, points=float(live.live_fpts) if scored else None,
        )

    held: dict[int, list[LineupPlayer]] = {}
    for p in state.players:
        held.setdefault(p.lineup_slot_id, []).append(p)
    seats: list[OverviewSeat] = []
    for slot in state.slots:
        if slot.slot_id in (BENCH_SLOT_ID, IR_SLOT_ID):
            continue
        holders = held.get(slot.slot_id, [])
        seats += [seat(p) for p in holders]
        seats += [OverviewSeat(slot_id=slot.slot_id, slot=slot.slot, mark="empty")
                  for _ in range(max(0, slot.count - len(holders)))]
    if not state.slots:      # a board that sent no slot rows: the players it has, in slot order
        seats = [seat(p) for p in sorted(state.players, key=lambda p: p.lineup_slot_id)
                 if p.lineup_slot_id not in (BENCH_SLOT_ID, IR_SLOT_ID)]

    counts = {m: 0 for m in MARKS}
    for s in seats:
        counts[s.mark] += 1
    scores = [s.points for s in seats if s.points is not None]
    to_come = sorted(p.game_time_et for p in state.players
                     if p.lineup_slot_id != IR_SLOT_ID and p.has_game_today and not p.game_started and p.game_time_et)
    next_lock = to_come[0] if to_come else None
    return OverviewTonight(
        first_tip_et=state.first_game_time_et, next_lock_et=next_lock,
        next_lock_at=tip_at(state.nba_date, next_lock), seats=seats,
        bench=[seat(p) for p in held.get(BENCH_SLOT_ID, [])], ir=[seat(p) for p in held.get(IR_SLOT_ID, [])],
        counts=counts, points=round(sum(scores), 1) if scores else None,
    )


# ------------------------------- the matchup and its week (pure) ------------------------------- #


def _starts(p: Any) -> bool:
    return getattr(p, "lineup_slot", None) not in ("BE", "IR")


def points_today(roster: Iterable[Any]) -> float:
    """What the starters have scored tonight: the live score's own rule (in progress and final)."""
    return round(sum(float(p.live.live_fpts) for p in roster
                     if _starts(p) and getattr(p, "live", None) is not None and p.live.game_status >= 2), 1)


def matchup_of(live: Any, win_chance: Optional[float]) -> OverviewMatchup:
    cats = live.category_comparison
    return OverviewMatchup(
        matchup_period=live.matchup_period, start=live.matchup_period_start, end=live.matchup_period_end,
        opponent_name=live.opponent_team.team_name, your_score=live.your_team.current_score,
        opponent_score=live.opponent_team.current_score, your_projected=live.your_team.projected_score,
        opponent_projected=live.opponent_team.projected_score, scoring_format=live.scoring_format,
        categories=OverviewCategories(wins=cats.wins, losses=cats.losses, ties=cats.ties) if cats else None,
        win_chance=win_chance,
    )


def _could_play(roster: Iterable[Any], on_ir: set[int], seats: Optional[int]) -> int:
    n = sum(1 for p in roster if p.has_game and p.player_id not in on_ir
            and (p.injury_status or "ACTIVE").upper() not in OUT_STATUSES)
    return min(n, seats) if seats else n


def week_of(days: Sequence[Any], live: Any, seats: Optional[int]) -> list[OverviewDay]:
    """The matchup's days for the row's strip: each side's points on the days played, and its
    games on every day. `days` are the weekly matchup's (`WeeklyMatchupData.days`); `live` is
    the live matchup, for today's points and who starts tonight; `seats` caps a count taken
    off a whole roster at the league's starting seats."""
    points_league = live.scoring_format == "points"
    sides = (live.your_team.roster, live.opponent_team.roster)
    starters = [{p.player_id for p in roster if _starts(p)} for roster in sides]
    on_ir = [{p.player_id for p in roster if getattr(p, "lineup_slot", None) == "IR"} for roster in sides]
    out: list[OverviewDay] = []
    for d in days:
        teams = (d.your_team, d.opponent_team)
        if d.day_type == "future":
            games = [_could_play(t.roster, on_ir[i], seats) for i, t in enumerate(teams)]
            out.append(OverviewDay(date=d.date, kind="future", your_games=games[0], opponent_games=games[1]))
            continue
        if d.day_type == "today":
            games = [sum(1 for p in t.roster if p.had_game and p.player_id in starters[i])
                     for i, t in enumerate(teams)]
            scored = [points_today(roster) for roster in sides] if points_league else [None, None]
            out.append(OverviewDay(date=d.date, kind="today", your_points=scored[0], opponent_points=scored[1],
                                   your_games=games[0], opponent_games=games[1]))
            continue
        games, estimated = [], False
        for t in teams:
            dated = t.roster_source != "current"
            n = sum(1 for p in t.roster if p.had_game and (not dated or counts_for_the_day(p)))
            if not dated:
                estimated = True
                n = min(n, seats) if seats else n
            games.append(n)
        scored = [round(float(t.total_fpts), 1) if points_league and t.total_fpts is not None else None
                  for t in teams]
        out.append(OverviewDay(date=d.date, kind="past", your_points=scored[0], opponent_points=scored[1],
                               your_games=games[0], opponent_games=games[1], estimated=estimated))
    return out


def starting_seats(state: Optional[LineupState]) -> Optional[int]:
    """The league's starting seats, off the board; None without one."""
    if state is None:
        return None
    n = sum(int(v) for k, v in state.slot_counts.items() if int(k) not in (BENCH_SLOT_ID, IR_SLOT_ID))
    return n or None


# ------------------------------- the season (pure) ------------------------------- #


def _field(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)


def season_of(projection: Mapping[str, Any], as_of: datetime) -> tuple[Optional[OverviewSeason], Optional[float]]:
    """The user's own line of a league projection, and this matchup's win chance."""
    mine = next((t for t in projection.get("teams") or [] if _field(t, "mine")), None)
    if mine is None:
        return None, None
    current = next((m for m in projection.get("matchups") or [] if _field(m, "status") == "current"), None)
    season = OverviewSeason(
        wins=_field(mine, "wins") or 0, losses=_field(mine, "losses") or 0, ties=_field(mine, "ties") or 0,
        standing=_field(mine, "standing"), teams=projection.get("teams_count") or 0,
        playoff_teams=projection.get("playoff_teams") or 0, playoff_odds=_field(mine, "playoff_odds"), as_of=as_of,
    )
    return season, (_field(current, "win_chance") if current is not None else None)


# ------------------------------- what's next (pure) ------------------------------- #


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def next_of(now: datetime, *, fill_at: Optional[datetime] = None, pickups: Sequence[Any] = (),
            lanes: Sequence[Any] = (), checkin: Any = None) -> Optional[OverviewNext]:
    """The soonest thing scheduled on the team. Something already due is due now."""
    found: list[OverviewNext] = []
    if fill_at is not None:
        found.append(OverviewNext(kind="auto_lineup", at=max(fill_at, now), label="Auto-lineup sets the lineup"))
    for p in pickups:
        at = _aware(p.next_attempt_at) or _aware(p.not_before_at)
        if at is None:
            continue
        label = f"Add {p.add_name}" + (f", drop {p.drop_name}" if p.drop_name else "")
        found.append(OverviewNext(kind="pickup", at=max(at, now), label=label))
    for lane in lanes:
        at = _aware(lane.next_decision_at)
        if at is None or lane.status != "active":
            continue
        found.append(OverviewNext(kind="lane", at=max(at, now),
                                  label=f"Stream lane decides on {lane.holder.name}'s spot"))
    if checkin is not None:
        found.append(OverviewNext(kind="checkin", at=max(_aware(checkin.due_at), now),
                                  label=checkins.LABELS.get(checkin.kind, "Check-in")))
    return min(found, key=lambda n: n.at) if found else None


# ------------------------------- what is kept ------------------------------- #


@dataclass(frozen=True)
class _Kept:
    value: Any
    at: float                # monotonic seconds
    epoch: int               # the write epoch it was read in
    ttl: float


class _Keep:
    """A small TTL keep, dropped by any provider write this process makes when `fenced`."""

    def __init__(self, *, fenced: bool):
        self.fenced = fenced
        self._rows: OrderedDict[Any, _Kept] = OrderedDict()

    def get(self, key: Any, *, stale: bool = False) -> Optional[_Kept]:
        kept = self._rows.get(key)
        if kept is None or (self.fenced and kept.epoch != write_epoch()):
            return None
        if not stale and clock.monotonic() - kept.at > kept.ttl:
            return None
        return kept

    def put(self, key: Any, value: Any, ttl: float, epoch: Optional[int] = None) -> None:
        self._rows[key] = _Kept(value, clock.monotonic(), write_epoch() if epoch is None else epoch, ttl)
        self._rows.move_to_end(key)
        while len(self._rows) > MAX_KEPT:
            self._rows.popitem(last=False)

    def clear(self) -> None:
        self._rows.clear()


@dataclass(frozen=True)
class _Season:
    projection: Optional[Mapping[str, Any]]      # None: this league has none (or its read failed)
    as_of: datetime


_overviews = _Keep(fenced=True)
_weeks = _Keep(fenced=True)
_seasons = _Keep(fenced=False)
_building = InFlight("teams_overview")
_projecting: dict[int, asyncio.Task] = {}
_gate: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = None


def clear() -> None:
    """Drop everything kept (tests)."""
    _overviews.clear()
    _weeks.clear()
    _seasons.clear()
    _projecting.clear()


def _one_at_a_time() -> asyncio.Lock:
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        _gate = (loop, asyncio.Lock())
    return _gate[1]


# ------------------------------- reads ------------------------------- #


@dataclass(frozen=True)
class Account:
    """What every team of the account shares in one overview."""

    user_id: int
    now: datetime
    today: date
    tip_at: Optional[datetime]                   # today's first NBA tip
    prefs: Any                                   # the account's notification preferences
    team_prefs: Mapping[int, Any]                # each team's override
    autopilot: Mapping[int, Any]                 # each team's usr.autopilot row (absent: never switched on)
    dime: bool                                   # CV AI access, and the server's switches on
    calendar: Optional[checkins.Calendar]        # the check-in schedule, when some team has check-ins on


def _owned_teams(user_id: int) -> list[OwnedTeamContext]:
    ids = [t for (t,) in Team.select(Team.team_id).where(Team.user_id == user_id).order_by(Team.team_id).tuples()]
    return [team for team in (ensure_team_owned(t, user_id) for t in ids) if team is not None]


def _first_tip(today: date) -> Optional[time]:
    return Game.get_earliest_game_time_on_date(today)


async def _account(user_id: int, team_ids: list[int], now: datetime) -> Account:
    today = checkins.fantasy_day(now)
    tip = await run_db("teams_overview.first_tip", _first_tip, today)
    prefs, team_prefs = await levels.prefs_of(user_id, team_ids)
    rows = {t: row for t, row in await checkins._autopilot_teams(user_id, team_ids) if row is not None}
    dime = user_id in settings.ai_agent_user_ids and autonomy.acting_available()
    calendar = None
    if dime and any(autonomy.checkins_on(r) for r in rows.values()):
        try:
            calendar = await checkins.calendar(now)
        except Exception as exc:       # without it no check-in is counted on, and none is shown as next
            log.warning("teams_overview_calendar_failed", error=f"{type(exc).__name__}: {exc}")
    return Account(user_id=user_id, now=now, today=today, tip_at=et_at(today, tip) if tip is not None else None,
                   prefs=prefs, team_prefs=team_prefs, autopilot=rows, dime=dime, calendar=calendar)


@dataclass(frozen=True)
class _Plan:
    """What is scheduled on one team, and what its automations wrote today."""

    writes: Sequence[Any] = ()
    pickups: Sequence[Any] = ()
    lanes: Sequence[Any] = ()
    checkin: Any = None


def _team_rows(team_id: int, today: date) -> tuple[list[Any], list[Any]]:
    return _todays_writes(team_id, today), _pending_pickups(team_id)


async def _plan(team: OwnedTeamContext, acct: Account) -> _Plan:
    writes, pickups = await run_db("teams_overview.team_rows", _team_rows, team.team_id, acct.today)
    lanes = (await StreamLaneService.list_for_team(team)).data
    checkin = None
    row = acct.autopilot.get(team.team_id)
    if acct.calendar is not None and autonomy.checkins_on(row):
        days = sorted({s.nba_date for s in acct.calendar.slots()})
        taken = await checkins._taken(team.team_id, days)
        checkin = checkins.next_slot(acct.calendar, row, taken, acct.now)
    return _Plan(writes=writes, pickups=pickups, lanes=list(lanes.open) if lanes else [], checkin=checkin)


def rules_of(team_id: int, acct: Account, plan: Optional[_Plan], view: Mapping[str, str], *,
             can_write: bool, board_date: Optional[str]) -> Rules:
    """The team's rules today. Without its plan (the read failed) nothing is counted as covered
    by the fill, since whether it ran is unknown."""
    prefs, team_pref, row = acct.prefs, acct.team_prefs.get(team_id), acct.autopilot.get(team_id)
    fill = fill_setting(prefs, team_pref)
    writes = list(plan.writes) if plan is not None else []
    fill_ran = any(w.source == "auto" and w.status in COUNTED_AUTO_STATUSES for w in writes)
    fill_failed = not fill_ran and any(w.source == "auto" and w.status in FAILED_STATUSES for w in writes)
    swaps = [w for w in writes if w.source == "availability" and w.trigger_player_id is not None]
    swapped = {w.trigger_player_id for w in swaps if w.status in COUNTED_AUTO_STATUSES}
    scratch_failed = {w.trigger_player_id for w in swaps if w.status in FAILED_STATUSES} - swapped
    # the fill's clock is today's; a board on another day (ESPN's own, around 3 AM ET) isn't its
    on_today = board_date == acct.today.isoformat()
    return Rules(
        can_write=can_write,
        late_scratch=view.get(autonomy.LATE_SCRATCH, autonomy.OFF),
        daily_actions=view.get(autonomy.DAILY_ACTIONS, autonomy.OFF),
        fill_applies=fill.applies and plan is not None and on_today,
        fill_ran=fill_ran,
        fill_closes_at=acct.tip_at - FILL_WINDOW_CLOSES if acct.tip_at is not None else None,
        dime_acts=frozenset(autonomy.acting_rows(row)) if acct.dime else frozenset(),
        next_checkin_at=_aware(plan.checkin.due_at) if plan is not None and plan.checkin is not None else None,
        fill_failed=fill_failed,
        scratch_failed=frozenset(scratch_failed),
    )


def levels_of(team_id: int, acct: Account) -> dict[str, str]:
    """Each automation's level on the team, as the settings show it."""
    row = acct.autopilot.get(team_id)
    view = checkins.team_view(team_id, row, None, acct.prefs, acct.team_prefs.get(team_id))
    return {r["row"]: r["level"] for r in view["rows"]}


def _fill_at(acct: Account, rules: Rules, team_id: int) -> Optional[datetime]:
    """When today's auto-lineup is due on the team; None when it won't run (or already has)."""
    if not rules.fill_applies or rules.fill_ran or not rules.can_write or acct.tip_at is None:
        return None
    if rules.fill_closes_at is None or acct.now >= rules.fill_closes_at:
        return None
    minutes = fill_setting(acct.prefs, acct.team_prefs.get(team_id)).minutes_before
    return acct.tip_at - timedelta(minutes=minutes)


@dataclass(frozen=True)
class _MatchupRead:
    live: Any                                    # LiveMatchupData
    days: Optional[Sequence[Any]]                # the weekly matchup's days; None when they couldn't be built


async def _matchup(team: OwnedTeamContext) -> Optional[_MatchupRead]:
    """One provider read of the matchup, under tonight's box scores and the week's days. None when
    the team has no matchup now (a bye, the offseason)."""
    user_id, team_id = team.user_id, team.team_id
    epoch = matchup_hold.reading()
    matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")
    if matchup.status != ApiStatus.SUCCESS or not matchup.data:
        return None
    kept = matchup_hold.keep(user_id, team_id, matchup, epoch)
    live = await MatchupService.get_live_matchup_by_team_id(user_id, team_id, reuse_from=kept.read_ms)
    if live.status != ApiStatus.SUCCESS or live.data is None:
        return None
    days = None
    held = _weeks.get(team_id)
    if held is not None and held.value[0] == live.data.matchup_period:
        days = held.value[1]
    else:
        try:
            weekly = await MatchupService.weekly_of(team_id, matchup.data)
            if weekly.data is not None:
                days = weekly.data.days
                _weeks.put(team_id, (live.data.matchup_period, days), WEEK_TTL, epoch)
        except Exception as exc:       # the score still shows; the strip is what's missing
            log.warning("teams_overview_week_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
    return _MatchupRead(live=live.data, days=days)


async def _project(team_id: int, league_info: LeagueInfo) -> None:
    now = datetime.now(timezone.utc)
    try:
        async with _one_at_a_time():
            projection = await league_projection(league_info)
        _seasons.put(team_id, _Season(projection, datetime.now(timezone.utc)), SEASON_TTL)
    except LeagueProjectionUnavailable:
        _seasons.put(team_id, _Season(None, now), SEASON_TTL)
    except Exception as exc:
        log.warning("teams_overview_projection_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
        old = _seasons.get(team_id, stale=True)
        # the one it was replacing stands until the retry; with none, the league has none for now
        _seasons.put(team_id, old.value if old is not None else _Season(None, now), SEASON_RETRY)
    finally:
        _projecting.pop(team_id, None)


async def _season(team: OwnedTeamContext, league_info: LeagueInfo) -> tuple[str, Optional[_Season]]:
    """The league's projection for the team: ("ready", it), ("pending", None) while the first one is
    still being made, ("unavailable", None) for a league that has none. An old one stands in
    while its replacement is made."""
    scoring = team.league.scoring_type if team.league is not None else None
    if league_info.provider != FantasyProvider.ESPN or scoring in ("categories", "roto"):
        return "unavailable", None
    team_id = team.team_id
    fresh = _seasons.get(team_id)
    if fresh is None:
        task = _projecting.get(team_id)
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            task = _projecting[team_id] = asyncio.create_task(_project(team_id, league_info))
        await asyncio.wait({task}, timeout=SEASON_BUDGET)
        fresh = _seasons.get(team_id)
    held = fresh or _seasons.get(team_id, stale=True)      # an old one stands in for one still being made
    if held is None:
        return "pending", None
    if held.value.projection is None:
        return "unavailable", None
    return "ready", held.value


# ------------------------------- one team ------------------------------- #


def _error_of(exc: BaseException) -> OverviewError:
    if isinstance(exc, AppError):
        return OverviewError(code=exc.error_code, message=exc.message)
    return OverviewError(code="TEAM_READ_FAILED", message="This team could not be read")


async def _soft(name: str, team_id: int, read: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
    """A block's read: (its value, True), or (None, False) when it failed."""
    try:
        return await read(), True
    except Exception as exc:
        log.warning("teams_overview_block_failed", block=name, team_id=team_id,
                    error=f"{type(exc).__name__}: {exc}")
        return None, False


def _names(team: OwnedTeamContext) -> dict[str, Any]:
    """Who the team is, off the stored row: no credentials, so a team that can't be read has its name."""
    try:
        stored = json.loads(team.league_info_json or "{}")
    except ValueError:
        stored = {}
    league = team.league
    scoring = (league.scoring_preview or league.scoring_type) if league is not None else stored.get("scoring_preview")
    try:
        provider = FantasyProvider(stored.get("provider") or "espn")
    except ValueError:
        provider = FantasyProvider.ESPN
    stored_league = stored.get("league_name")
    return {
        "team_id": team.team_id,
        "team_name": stored.get("team_name") or f"Team {team.team_id}",
        "league_name": (league.name if league is not None and league.name else None)
        or (stored_league if stored_league and stored_league != "N/A" else None),
        "provider": provider,
        "scoring_format": scoring if scoring in ("points", "categories") else None,
    }


async def team_overview(team: OwnedTeamContext, acct: Account) -> TeamOverview:
    """One team's line of the overview. Never raises: a team that can't be read says so itself."""
    team_id, now = team.team_id, acct.now
    try:
        league_info = await load_owned_league_info(team)
    except Exception as exc:
        log.warning("teams_overview_credentials_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
        return TeamOverview(**_names(team), state="cant_read", error=_error_of(exc))

    async def board() -> Any:
        """The day's actions, or the reason the board couldn't be read (the team's `cant_read`)."""
        try:
            return (await DailyActionsService.read(team, league_info)).data
        except Exception as exc:
            log.warning("teams_overview_board_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
            return exc

    actions, (matchup, matchup_ok), (plan, plan_ok), (season, _) = await asyncio.gather(
        board(),
        _soft("matchup", team_id, lambda: _matchup(team)),
        _soft("next", team_id, lambda: _plan(team, acct)),
        _soft("season", team_id, lambda: _season(team, league_info)),
    )
    missing: list[str] = []

    view, row = None, acct.autopilot.get(team_id)
    try:
        view = levels_of(team_id, acct)
    except Exception as exc:
        log.warning("teams_overview_levels_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
        missing.append("automation")
    automation = None
    if view is not None:
        automation = OverviewAutomation(
            autopilot=autonomy.switch_on(row), dime=acct.dime and autonomy.checkins_on(row),
            lineup=view.get(autonomy.LINEUP, autonomy.OFF),
            daily_actions=view.get(autonomy.DAILY_ACTIONS, autonomy.OFF),
            lanes=view.get(autonomy.LANES, autonomy.OFF),
            late_scratch=view.get(autonomy.LATE_SCRATCH, autonomy.OFF),
        )

    season_state, season_row, win_chance = "unavailable", None, None
    if season is not None:
        season_state, held = season
        if held is not None:
            season_row, win_chance = season_of(held.projection, held.as_of)
            if season_row is None:
                season_state = "unavailable"

    live = matchup.live if matchup is not None else None
    if not matchup_ok:
        missing.append("matchup")
    unread = actions is None or isinstance(actions, Exception)
    lineup: Optional[LineupState] = None if unread else actions.lineup
    week: list[OverviewDay] = []
    if live is not None:
        if matchup.days is not None:
            week = week_of(matchup.days, live, starting_seats(lineup))
        else:
            missing.append("week")
    if not plan_ok:
        missing.append("next")

    out: dict[str, Any] = dict(
        **_names(team), automation=automation,
        matchup=matchup_of(live, win_chance) if live is not None else None, week=week,
        season=season_row, season_state=season_state, missing=missing,
    )
    if live is not None:
        out["scoring_format"] = live.scoring_format

    if unread:
        # the board decides: without it the team's state is unknown, whatever else was read
        error = _error_of(actions) if actions is not None else _error_of(RuntimeError("no board"))
        return TeamOverview(**out, state="cant_read", error=error)
    if lineup is None:
        # a provider whose lineups Court Vision can't read yet: the score, and nothing to judge
        error = OverviewError(code="LINEUP_NOT_SUPPORTED",
                              message=unavailable_message("daily_actions", league_info.provider))
        return TeamOverview(**out, state="cant_read", error=error)

    rules = rules_of(team_id, acct, plan, view or {}, can_write=actions.can_write, board_date=actions.nba_date)
    items = items_of(actions, rules, now)
    rows, teams = game_states([*live.your_team.roster, *live.opponent_team.roster]) if live is not None else ({}, {})
    points_league = (live.scoring_format if live is not None else out["scoring_format"]) != "categories"
    upcoming = None
    if plan is not None:
        upcoming = next_of(now, fill_at=_fill_at(acct, rules, team_id), pickups=plan.pickups, lanes=plan.lanes,
                           checkin=plan.checkin)
    runs_itself = acct.dime and autonomy.switch_on(row) and autonomy.checkins_on(row)
    return TeamOverview(
        **out, state=state_of(items, autopilot=runs_itself), items=items,
        tonight=tonight_of(lineup, rows, teams, now, points_league=points_league), next=upcoming,
        nba_date=actions.nba_date, roster_version=actions.roster_version,
        scoring_period_id=actions.scoring_period_id, can_write=actions.can_write,
        write_blocked_reason=actions.write_blocked_reason,
    )


# ------------------------------- every team ------------------------------- #


def order_of(team: TeamOverview) -> tuple[int, datetime, int]:
    """Needs you first, soonest deadline first; then can't read, your call, set, autopilot."""
    deadlines = [i.deadline_at for i in team.items if i.standing == "needs_you" and i.deadline_at is not None]
    return _STATE_ORDER[team.state], min(deadlines) if deadlines else _FAR, team.team_id


def summarize(teams: list[TeamOverview], now: datetime) -> TeamsOverviewData:
    counts = {s: 0 for s in STATES}
    for t in teams:
        counts[t.state] += 1
    deadlines = [i.deadline_at for t in teams for i in t.items
                 if i.standing == "needs_you" and i.deadline_at is not None]
    return TeamsOverviewData(as_of=now, counts=counts, next_deadline_at=min(deadlines) if deadlines else None,
                             teams=sorted(teams, key=order_of))


async def _build(user_id: int, now: datetime) -> TeamsOverviewData:
    teams = await run_db("teams_overview.teams", _owned_teams, user_id)
    if not teams:
        return summarize([], now)
    acct = await _account(user_id, [t.team_id for t in teams], now)
    room = asyncio.Semaphore(TEAMS_AT_ONCE)

    started = clock.perf_counter()
    took: dict[int, int] = {}

    async def one(team: OwnedTeamContext) -> TeamOverview:
        async with room:
            began = clock.perf_counter()
            try:
                return await team_overview(team, acct)
            except Exception as exc:       # nothing above should raise; if it does, it is this team's alone
                log.exception("teams_overview_team_failed", team_id=team.team_id)
                return TeamOverview(**_names(team), state="cant_read", error=_error_of(exc))
            finally:
                took[team.team_id] = round((clock.perf_counter() - began) * 1000)

    data = summarize(list(await asyncio.gather(*(one(t) for t in teams))), now)
    log.info("teams_overview_built", user_id=user_id, teams=len(teams), counts=data.counts,
             elapsed_ms=round((clock.perf_counter() - started) * 1000), team_ms=took)
    return data


async def overview(user_id: int, now: Optional[datetime] = None) -> TeamsOverviewData:
    """Every team of the account, each with its state. Kept OVERVIEW_TTL, and never across a
    provider write this process made."""
    kept = _overviews.get(user_id)
    if kept is not None:
        return kept.value

    async def build(_seal: Any) -> TeamsOverviewData:
        epoch = write_epoch()
        data = await _build(user_id, now or datetime.now(timezone.utc))
        _overviews.put(user_id, data, OVERVIEW_TTL, epoch)
        return data

    return await _building.run((user_id, write_epoch()), build)
