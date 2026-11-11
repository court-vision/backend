"""
Dime's event check-ins (usr.autopilot_events, migration 0045; the Dime autopilot plan,
step 6).

The scheduled check-ins (services/ai/checkins.py) run on a clock. This is what
starts one when something happens instead: a player's availability verdict
changes before his game, or the news feed stores an item about his health, role
or roster spot. data-platform posts both (POST /v1/internal/jobs/checkins/events):
the availability pipeline every 2-minute tick that resolved a verdict change for
a game not yet tipped (its late-scratch events), the news feed every 5 minutes
for the items it stored. They carry keys only; the facts are read here from the
tables the pipelines wrote.

For each team Dime runs (live, allowlisted) whose Late scratches row is at AI
(the user put it there, or the autopilot switch is on; services/ai/autonomy.py),
an event is recorded for each fact about a player on its roster (read from ESPN,
kept for a few minutes), once per verdict change or news item. Then the team is
kicked:

- a scheduled check-in that is due now, or opens within
  AUTOPILOT_EVENT_FOLD_MINUTES, takes the events (its brief lists them) rather
  than another turn starting for them, unless a player's game tips before that
  check-in could answer;
- otherwise an event check-in starts for them, within its own daily cap
  (AUTOPILOT_EVENT_CHECKINS_PER_DAY) and never in a scheduled check-in's place
  under AUTOPILOT_CHECKINS_PER_DAY (a rolling day, so tomorrow morning's are
  owed room too);
- a team with a check-in running waits: that one's finish kicks again, as does
  every 15-minute tick.

Every check-in takes the team's pending events when it starts, so nothing is
answered twice and nothing waits past the next one. An event that can no longer
be answered is dropped: his game tipped (`tipped`), news more than half a day
old (`stale`), or the row left AI before a check-in took it (`level`). What an
event check-in commits counts under Late scratches (autonomy.rows_of), so it is
sent only where that row acts.

There is no switch for events alone: they follow the team (AUTOPILOT_TEAM_IDS and
its Late scratches row). With AI_ENABLED or AUTOPILOT_ENABLED off nothing is
recorded and no check-in takes an event.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from core.logging import get_logger
from core.nba_calendar import EASTERN
from core.settings import settings
from db.base import db_operation, run_db
from db.models.autopilot import AiCheckin, AutopilotEvent
from db.models.nba.availability import PlayerAvailability
from db.models.nba.games import Game
from db.models.nba.players import Player
from services import news_service
from services.ai import autonomy
from services.scheduled_pickup_service import et_at

AVAILABILITY, NEWS = "availability", "news"
PENDING, TAKEN, DROPPED = "pending", "taken", "dropped"
# The verdict changes worth a check-in: the late-scratch reactor's (data-platform's
# `is_reaction_event`): ruled out, doubtful, or cleared
VERDICTS = ("out", "doubtful", "available")
# News about a player's health, role or roster spot; a box score recap or a rumor is not an event
NEWS_KINDS = ("injury", "availability", "lineup", "transaction")
# An item older than this when it arrives is not news (a feed's first run fills its backlog)
NEWS_FRESH = timedelta(hours=3)
# A news event still pending this long is dropped
NEWS_STALE = timedelta(hours=12)
# A player whose game tips this soon after the next scheduled check-in opens can't wait for it
URGENT_MARGIN = timedelta(minutes=15)
# AUTOPILOT_CHECKINS_PER_DAY counts a rolling day
DAY = timedelta(days=1)

VERDICT_WORDS = {
    "out": "ruled out",
    "doubtful": "listed doubtful",
    "questionable": "listed questionable",
    "probable": "listed probable",
    "available": "cleared to play",
    "unknown": "status unknown",
}
SOURCE_WORDS = {
    "nba_boxscore": "the NBA box score",
    "nba_report": "the NBA injury report",
    "espn_injuries": "ESPN's injury list",
    "espn_fantasy": "ESPN fantasy",
    "news_claim": "a news report",
    "espn_player_news": "ESPN",
    "espn_site_news": "ESPN",
    "rotowire_rss": "Rotowire",
    "realgm_rss": "RealGM",
}


def enabled() -> bool:
    """The server's kill switches; which teams take events is `watches` and the allowlist."""
    return autonomy.acting_available()


def watches(row: Any) -> bool:
    """The team takes events: its Late scratches row is at AI (the user put it there, or the
    autopilot switch is on). Below AI nothing is recorded for it, no event check-in starts, and
    what was still waiting is dropped when its next check-in starts (`level`)."""
    return autonomy.at_ai(row, autonomy.LATE_SCRATCH)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _clock(moment: datetime) -> str:
    et = moment.astimezone(EASTERN)
    return et.strftime("%I:%M %p").lstrip("0") + " ET"


# ------------------------------- the facts ------------------------------- #


@dataclass(frozen=True)
class Fact:
    """One thing that came in about one player, ready to be an event."""

    source: str
    player_id: int
    summary: str
    game_date: Optional[date] = None
    game_id: Optional[str] = None
    tip_at: Optional[datetime] = None
    verdict: Optional[str] = None
    previous_verdict: Optional[str] = None
    changed_at: Optional[datetime] = None
    news_item_id: Optional[int] = None
    news_kind: Optional[str] = None


def verdict_summary(name: str, verdict: str, previous: Optional[str], decided_by: Optional[str],
                    reason: Optional[str], game_date: date, matchup: Optional[str], tip_at: Optional[datetime],
                    changed_at: Optional[datetime]) -> str:
    """'Jalen Brunson ruled out for Thu Nov 5, BOS @ NYK (7:30 PM ET tip), per ESPN fantasy at 6:12 PM ET;
    was questionable. Left Knee'"""
    day = f"{game_date.strftime('%a %b')} {game_date.day}"
    game = f"{day}, {matchup}" if matchup else day
    if tip_at is not None:
        game += f" ({_clock(tip_at)} tip)"
    words = VERDICT_WORDS.get(verdict, verdict)
    line = f"{name} {words} for {game}"
    if decided_by:
        line += f", per {SOURCE_WORDS.get(decided_by, decided_by)}"
        if changed_at is not None:
            line += f" at {_clock(changed_at)}"
    if previous and previous != verdict:
        line += f"; was {previous}"
    if reason:
        line += f". {reason}"
    return line[:400]


def news_summary(name: str, headline: str, source: str, published_at: Optional[datetime]) -> str:
    """'Jalen Brunson: <headline> (Rotowire, 5:40 PM ET)'"""
    when = f", {_clock(published_at)}" if published_at is not None else ""
    tail = f" ({SOURCE_WORDS.get(source, source)}{when})"
    head = headline if headline.lower().startswith(name.lower()) else f"{name}: {headline}"
    room = 400 - len(tail)
    if len(head) > room:
        head = head[: room - 1].rstrip() + "…"
    return head + tail


def _games(keys: set[tuple[str, date]]) -> dict[str, Game]:
    ids = sorted({g for g, _ in keys if g})
    return {g.game_id: g for g in Game.select().where(Game.game_id.in_(ids))} if ids else {}


def _matchup(game: Game) -> str:
    return f"{game.away_team_id} @ {game.home_team_id}"


@db_operation("ai.event_facts")
def facts(availability: Sequence[tuple[int, date]], news: Sequence[int], now: datetime) -> list[Fact]:
    """The facts behind the posted keys: each verdict that changed to out, doubtful or available for
    a game that hasn't tipped, and each fresh news item of an event kind, once per player it is about."""
    out: list[Fact] = []
    keys = sorted(set(availability))
    if keys:
        rows = []
        for pid, day in keys:
            row = PlayerAvailability.get_or_none(PlayerAvailability.player == pid, PlayerAvailability.game_date == day)
            if row is not None:
                rows.append(row)
        games = _games({(r.game_id, r.game_date) for r in rows if r.game_id})
        names = dict(Player.select(Player.id, Player.name)
                     .where(Player.id.in_([r.player_id for r in rows])).tuples()) if rows else {}
        for r in rows:
            game = games.get(r.game_id) if r.game_id else None
            if r.verdict not in VERDICTS or r.verdict == r.previous_verdict or game is None:
                continue
            tip = et_at(game.game_date, game.start_time_et) if game.start_time_et else None
            if tip is None or tip <= now or game.status not in (None, "scheduled"):
                continue
            name = names.get(r.player_id, f"Player {r.player_id}")
            changed = _aware(r.changed_at)
            out.append(Fact(
                source=AVAILABILITY, player_id=r.player_id, game_date=r.game_date, game_id=r.game_id, tip_at=tip,
                verdict=r.verdict, previous_verdict=r.previous_verdict, changed_at=changed,
                summary=verdict_summary(name, r.verdict, r.previous_verdict, r.decided_by, r.reason, r.game_date,
                                        _matchup(game), tip, changed),
            ))
    if news:
        for item in news_service.items_by_id(list(news)).values():
            if item.kind not in NEWS_KINDS or item.published_at is None or now - item.published_at > NEWS_FRESH:
                continue
            for pid, name in item.players:
                out.append(Fact(source=NEWS, player_id=pid, news_item_id=item.id, news_kind=item.kind,
                                summary=news_summary(name, item.headline, item.source, item.published_at)))
    return out


# ------------------------------- the rows ------------------------------- #


@db_operation("ai.event_record")
def record(team_id: int, user_id: int, nba_date: date, found: Sequence[Fact], now: datetime) -> int:
    """Insert the team's events, once each (the partial unique indexes); returns how many are new."""
    new = 0
    for f in found:
        inserted = (AutopilotEvent.insert(
            team_id=team_id, user_id=user_id, source=f.source, player_id=f.player_id, nba_date=nba_date,
            game_date=f.game_date, game_id=f.game_id, tip_at=f.tip_at, verdict=f.verdict,
            previous_verdict=f.previous_verdict, changed_at=f.changed_at, news_item_id=f.news_item_id,
            news_kind=f.news_kind, summary=f.summary, received_at=now, state=PENDING)
            .on_conflict_ignore()
            .execute())
        new += inserted is not None
    return new


@db_operation("ai.event_pending")
def pending(team_id: int) -> list[AutopilotEvent]:
    return list(AutopilotEvent.select()
                .where(AutopilotEvent.team_id == team_id, AutopilotEvent.state == PENDING)
                .order_by(AutopilotEvent.received_at, AutopilotEvent.id))


@db_operation("ai.event_drop_stale")
def drop_stale(team_id: int, now: datetime) -> int:
    """Drop what can't be answered any more: a verdict whose game tipped, news half a day old."""
    tipped = (AutopilotEvent.update(state=DROPPED, reason="tipped", taken_at=now)
              .where(AutopilotEvent.team_id == team_id, AutopilotEvent.state == PENDING,
                     AutopilotEvent.source == AVAILABILITY, AutopilotEvent.tip_at <= now)
              .execute())
    stale = (AutopilotEvent.update(state=DROPPED, reason="stale", taken_at=now)
             .where(AutopilotEvent.team_id == team_id, AutopilotEvent.state == PENDING,
                    AutopilotEvent.source == NEWS, AutopilotEvent.received_at <= now - NEWS_STALE)
             .execute())
    return tipped + stale


@db_operation("ai.event_take")
def _take(team_id: int, checkin_id: int, now: datetime) -> list[AutopilotEvent]:
    ids = [e.id for e in AutopilotEvent.select(AutopilotEvent.id)
           .where(AutopilotEvent.team_id == team_id, AutopilotEvent.state == PENDING)]
    if not ids:
        return []
    (AutopilotEvent.update(state=TAKEN, checkin_id=checkin_id, taken_at=now)
     .where(AutopilotEvent.id.in_(ids), AutopilotEvent.state == PENDING)
     .execute())
    return list(AutopilotEvent.select()
                .where(AutopilotEvent.checkin_id == checkin_id, AutopilotEvent.state == TAKEN)
                .order_by(AutopilotEvent.received_at, AutopilotEvent.id))


@db_operation("ai.event_drop_all")
def _drop_all(team_id: int, now: datetime, reason: str) -> int:
    return (AutopilotEvent.update(state=DROPPED, reason=reason, taken_at=now)
            .where(AutopilotEvent.team_id == team_id, AutopilotEvent.state == PENDING)
            .execute())


async def take(team_id: int, checkin_id: int, now: Optional[datetime] = None, *, row: Any) -> list[AutopilotEvent]:
    """The team's pending events, now the check-in's (stale ones dropped first). Nothing while off;
    on a team whose Late scratches row is no longer at AI (`row`, its usr.autopilot), what was
    waiting is dropped instead."""
    if not enabled():
        return []
    now = now or _now()
    if not watches(row):
        await _drop_all(team_id, now, "level")
        return []
    await drop_stale(team_id, now)
    return await _take(team_id, checkin_id, now)


@db_operation("ai.event_release")
def release(checkin_id: int) -> int:
    """Give a check-in that failed to start its events back, for the next one."""
    return (AutopilotEvent.update(state=PENDING, checkin_id=None, taken_at=None)
            .where(AutopilotEvent.checkin_id == checkin_id, AutopilotEvent.state == TAKEN)
            .execute())


def of_checkins(checkin_ids: Sequence[int]) -> dict[int, list[AutopilotEvent]]:
    """Blocking: each check-in's events (the check-ins' summaries read them with their tallies)."""
    out: dict[int, list[AutopilotEvent]] = {}
    if not checkin_ids:
        return out
    for e in (AutopilotEvent.select()
              .where(AutopilotEvent.checkin_id.in_(list(checkin_ids)))
              .order_by(AutopilotEvent.received_at, AutopilotEvent.id)):
        out.setdefault(e.checkin_id_id, []).append(e)
    return out


def event_view(e: AutopilotEvent) -> dict[str, Any]:
    return {
        "source": e.source,
        "player_id": e.player_id,
        "summary": e.summary,
        "received_at": _aware(e.received_at),
        "verdict": e.verdict,
        "news_id": e.news_item_id,
    }


@db_operation("ai.event_checkins_on")
def _event_checkins_on(team_id: int, nba_date: date) -> int:
    from services.ai.checkins import EVENT, _RAN
    return (AiCheckin.select()
            .where(AiCheckin.team_id == team_id, AiCheckin.kind == EVENT, AiCheckin.nba_date == nba_date,
                   AiCheckin.state.in_(_RAN))
            .count())


# ------------------------------- the brief ------------------------------- #


def brief_lines(taken: Iterable[AutopilotEvent]) -> list[str]:
    """One line per event, with the player_id the tools take."""
    return [f"- {e.summary} (player_id {e.player_id})" for e in taken]


# ------------------------------- rosters ------------------------------- #

# team_id -> (monotonic expiry, the roster's NBA player ids)
_ROSTERS: dict[int, tuple[float, frozenset[int]]] = {}


async def roster_ids(team_id: int, user_id: int) -> Optional[frozenset[int]]:
    """The team's players (NBA ids) on ESPN's board, read at most once per
    AUTOPILOT_EVENT_ROSTER_TTL_SECONDS; the last read when ESPN can't be reached, else None."""
    cached = _ROSTERS.get(team_id)
    if cached is not None and cached[0] > _time.monotonic():
        return cached[1]
    from services.ai import acting, proposals
    try:
        team, league_info = await acting._team(team_id, user_id)
        if team is None:
            return None
        board = await proposals._board(team, league_info)
        ids = frozenset(p.nba_player_id for p in board.players if p.nba_player_id)
    except Exception as exc:
        get_logger().warning("ai_event_roster_failed", team_id=team_id, error=f"{type(exc).__name__}: {exc}")
        return cached[1] if cached is not None else None
    _ROSTERS[team_id] = (_time.monotonic() + settings.autopilot_event_roster_ttl_seconds, ids)
    return ids


def forget_roster(team_id: int) -> None:
    """A check-in that may have changed the roster drops its cached read."""
    _ROSTERS.pop(team_id, None)


# ------------------------------- receiving and kicking ------------------------------- #


async def receive(availability: Sequence[tuple[int, date]], news: Sequence[int],
                  now: Optional[datetime] = None) -> dict[str, Any]:
    """What data-platform posted: record each fact as an event on every team Dime runs that rosters
    the player, then kick each team that got a new one."""
    from services.ai import checkins
    now = now or _now()
    out: dict[str, Any] = {"off": False, "teams": 0, "facts": 0, "recorded": 0, "started": []}
    if not enabled():
        out["off"] = True
        return out
    # Teams Dime runs whose Late scratches row is at AI; the others hear nothing
    rows = [r for r in await checkins._live() if checkins.allowed(r.team_id_id, r.user_id_id) and watches(r)]
    out["teams"] = len(rows)
    if not rows or (not availability and not news):
        return out
    found = await facts(list(availability), list(news), now)
    out["facts"] = len(found)
    if not found:
        return out
    today = checkins.fantasy_day(now)
    for row in rows:
        team_id, user_id = row.team_id_id, row.user_id_id
        ids = await roster_ids(team_id, user_id)
        mine = [f for f in found if ids is not None and f.player_id in ids]
        if not mine:
            continue
        new = await record(team_id, user_id, today, mine, now)
        out["recorded"] += new
        if new:
            started = await kick(team_id, user_id, now, row=row)
            if started is not None:
                out["started"].append(started)
    get_logger().info("ai_events_received", teams=out["teams"], facts=out["facts"], recorded=out["recorded"],
                      started=len(out["started"]))
    return out


async def kick(team_id: int, user_id: int, now: Optional[datetime] = None, *, row: Any = None) -> Any:
    """Answer the team's pending events if a check-in may run now: the scheduled one that is due (it
    takes them), or an event check-in, unless one is running or opens soon, or the day's caps are
    used. Returns what it started (a checkins.Started), or None."""
    from services.ai import checkins
    if not enabled():
        return None
    now = now or _now()
    row = row if row is not None else await checkins._autopilot_row(team_id)
    if row is None or row.paused_at is not None or not watches(row) or not checkins.allowed(team_id, user_id):
        return None
    await drop_stale(team_id, now)
    waiting = await pending(team_id)
    if not waiting or await checkins._busy(team_id):
        return None
    cal = await checkins.calendar(now)
    # Today's check-ins and tomorrow's: the cap is a rolling day, so tomorrow morning's are owed
    # room too. The night follow-up is about the day before, so the claimed rows are read by each
    # slot's own day.
    slots = cal.slots() + cal.slots(cal.today + timedelta(days=1))
    taken = await checkins._taken(team_id, sorted({s.nba_date for s in slots}))
    ahead = [s for s in slots if (s.kind, s.nba_date) not in taken and now < s.closes_at
             and s.closes_at > _aware(row.active_since)]
    due = [s for s in ahead if s.due_at <= now]
    if due:
        # The scheduled check-in that is due anyway takes them
        return await checkins.start_slot(row, due[0], now)
    soon = [s for s in ahead if s.due_at - now <= timedelta(minutes=settings.autopilot_event_fold_minutes)]
    if soon:
        opens = soon[0].due_at
        urgent = any(e.tip_at is not None and _aware(e.tip_at) <= opens + URGENT_MARGIN for e in waiting)
        if not urgent:
            return None
    runs = await checkins._run_times(team_id, now - DAY)
    if not leaves_room(runs, ahead, now, settings.autopilot_checkins_per_day):
        return None
    if await _event_checkins_on(team_id, cal.today) >= settings.autopilot_event_checkins_per_day:
        return None
    return await checkins.start_event(row, cal.today, now)


def leaves_room(runs: Sequence[datetime], ahead: Sequence[Any], now: datetime, cap: int) -> bool:
    """Whether one more check-in started now stays under the rolling day's cap and leaves every
    scheduled one owed in the next day its place. When each falls due, the cap counts what ran in
    the day before it: the runs still inside that day, this one, and the owed ones due before it."""
    if len(runs) + 1 > cap:
        return False
    owed = sorted((s for s in ahead if s.due_at < now + DAY), key=lambda s: s.due_at)
    for before, slot in enumerate(owed):
        counted = sum(1 for at in runs if _aware(at) >= slot.due_at - DAY)
        if counted + 1 + before >= cap:
            return False
    return True
