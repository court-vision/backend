"""
The move ledger (the Dime autopilot plan, step 1): every add and drop each team
in a league made, and what it earned afterwards.

Moves are read off usr.lineup_snapshots, not off Court Vision's own writes: a
player on a team's day-N roster who was not on its previous stored day is an
add that counted from day N, and the other way round a drop. That sees every
move the same way, whoever made it (the desk, a scheduled pickup, a stream
lane, a proposal Dime made and the user sent, ESPN itself, a waiver claim, a
rival), and it sees only moves that really happened. Court Vision's own writes
(usr.roster_moves transactions that went through) then say who made a move:
a write counts from its own day or, sent after the first tip or the day before
for a scheduled pickup, up to two days later (`MATCH_DAYS`). A write's add and
drop are one paired move; the moves no write explains are `outside`, and the
day's lone add and lone drop on a team are paired with each other (a 1:1 swap,
or a 1-for-1 trade), anything less clear stays unpaired.

What a move earned is scored from the box scores (nba.player_game_stats) with
the league's own scoring, over three windows that start on the day it counted:
the day itself, the rest of that matchup, and two weeks. Each side's games are
all of its player's games in the window (the dropped player's are what the
team gave up), and the add also carries the games he sat in an active slot on
this team (`counted`), which is what the move put on the scoreboard. A window
that ends after the last box score is not `complete` yet; the nightly rebuild
fills it in.

The ledger is derived data: `rebuild_league` replaces a league's rows whole in
one transaction, so a rerun is always safe and the first run back-fills the
season from opening night. Run nightly by data-platform's `move_ledger`
pipeline (POST /v1/internal/jobs/ledger/run) after the lineup snapshots.
ESPN only, like the snapshots.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from core.logging import get_logger
from db.base import db
from db.models.ai_conversations import AiProposal
from db.models.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
from db.models.move_outcomes import MoveOutcome
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from services import schedule_service
from services.providers.identity import nba_ids_by_espn_id
from services.scoring.models import StatLine
from services.scoring.resolver import ResolvedScoring, resolve_scoring

log = get_logger("move_ledger")

ACTIVE_SLOT_IDS = frozenset(range(0, 12))      # ESPN: 12 = BE, 13 = IR
WINDOWS = ("day", "matchup", "two_weeks")
TWO_WEEKS = 14
MATCH_DAYS = 2                                  # a write counts from its day, or up to two days later
WRITTEN = ("applied", "applied_unverified")
ACTIVE_LEAGUE_DAYS = 21                         # leagues with a snapshot this recent are rebuilt
SEASON_MARGIN_DAYS = 60                         # writes this far outside a league's stored days are another season's


# ------------------------------- shapes ------------------------------- #


@dataclass(frozen=True, order=True)
class LeagueKey:
    provider: str
    provider_league_id: str
    season: int


@dataclass(frozen=True)
class DayRoster:
    """One team's stored roster on one ESPN day."""

    provider_team_id: int
    team_name: str
    scoring_period_id: int
    nba_date: date
    matchup_period_id: Optional[int]
    players: dict[int, tuple[str, int]]         # ESPN player id -> (name, lineup slot id)


@dataclass(frozen=True)
class Change:
    """A team's roster difference between two stored days."""

    day: DayRoster
    adds: tuple[tuple[int, str], ...]           # (ESPN id, name)
    drops: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class CvWrite:
    """A Court Vision add/drop that went through, and who made it."""

    roster_move_id: int
    team_id: int
    provider_team_id: int
    scoring_period_id: int
    source: str                                 # you | scheduled | lane | dime
    adds: frozenset[int]
    drops: frozenset[int]
    proposal_id: Optional[int] = None
    forecast: Optional[dict] = None
    reason: Optional[str] = None


@dataclass
class Move:
    day: DayRoster
    add: Optional[tuple[int, str]]
    drop: Optional[tuple[int, str]]
    source: str
    write: Optional[CvWrite] = None

    @property
    def paired(self) -> bool:
        return self.add is not None and self.drop is not None


@dataclass
class League:
    key: LeagueKey
    days: list[DayRoster]
    cv_teams: dict[int, int]                    # provider team id -> usr.teams id
    writes: list[CvWrite] = field(default_factory=list)


# ------------------------------- reading moves ------------------------------- #


def detect_changes(days: Iterable[DayRoster]) -> list[Change]:
    """Each team's adds and drops between consecutive stored days. A team's first stored
    day has nothing to compare with (the drafted roster), so it yields nothing."""
    by_team: dict[int, list[DayRoster]] = defaultdict(list)
    for day in days:
        by_team[day.provider_team_id].append(day)
    changes: list[Change] = []
    for team_days in by_team.values():
        team_days.sort(key=lambda d: d.scoring_period_id)
        for prev, cur in zip(team_days, team_days[1:]):
            adds = tuple(sorted((pid, cur.players[pid][0]) for pid in cur.players.keys() - prev.players.keys()))
            drops = tuple(sorted((pid, prev.players[pid][0]) for pid in prev.players.keys() - cur.players.keys()))
            if adds or drops:
                changes.append(Change(day=cur, adds=adds, drops=drops))
    changes.sort(key=lambda c: (c.day.scoring_period_id, c.day.provider_team_id))
    return changes


def attribute(changes: Iterable[Change], writes: Iterable[CvWrite]) -> list[Move]:
    """Turn roster changes into moves, each told by who made it.

    A Court Vision write explains the players it added or dropped when they changed on
    its own day or up to `MATCH_DAYS` later (the newest such write wins, and a write
    explains a player once). What no write explains is `outside`: a team's lone
    remaining add and lone remaining drop on a day are one paired move, anything else
    is one unpaired move per player."""
    by_team: dict[int, list[CvWrite]] = defaultdict(list)
    for w in writes:
        by_team[w.provider_team_id].append(w)
    for team_writes in by_team.values():
        team_writes.sort(key=lambda w: (w.scoring_period_id, w.roster_move_id), reverse=True)
    used: set[tuple[int, int]] = set()          # (roster_move_id, ESPN player id)

    moves: list[Move] = []
    for change in changes:
        period = change.day.scoring_period_id
        adds = dict(change.adds)
        drops = dict(change.drops)
        for w in by_team.get(change.day.provider_team_id, ()):
            if not (period - MATCH_DAYS <= w.scoring_period_id <= period):
                continue
            add_id = next((pid for pid in sorted(w.adds) if pid in adds and (w.roster_move_id, pid) not in used), None)
            drop_id = next((pid for pid in sorted(w.drops) if pid in drops and (w.roster_move_id, pid) not in used), None)
            if add_id is None and drop_id is None:
                continue
            add = (add_id, adds.pop(add_id)) if add_id is not None else None
            drop = (drop_id, drops.pop(drop_id)) if drop_id is not None else None
            used.update((w.roster_move_id, pid) for pid in (add_id, drop_id) if pid is not None)
            moves.append(Move(day=change.day, add=add, drop=drop, source=w.source, write=w))
        if len(adds) == 1 and len(drops) == 1:
            moves.append(Move(day=change.day, add=next(iter(adds.items())), drop=next(iter(drops.items())),
                              source="outside"))
        else:
            moves.extend(Move(day=change.day, add=item, drop=None, source="outside") for item in sorted(adds.items()))
            moves.extend(Move(day=change.day, add=None, drop=item, source="outside") for item in sorted(drops.items()))
    return moves


# ------------------------------- scoring ------------------------------- #


Games = dict[int, list[tuple[date, StatLine]]]    # NBA player id -> (game date, line)


def windows_of(start: date, matchup_end: Optional[date]) -> dict[str, tuple[date, date]]:
    end = matchup_end if matchup_end is not None and matchup_end >= start else start
    return {"day": (start, start), "matchup": (start, end), "two_weeks": (start, start + timedelta(days=TWO_WEEKS - 1))}


def _side(lines: list[StatLine], counted: list[StatLine], scoring: ResolvedScoring, with_counted: bool
          ) -> dict[str, Any]:
    total = StatLine.sum(lines)
    if scoring.is_categories and scoring.categories is not None:
        side: dict[str, Any] = {"games": len(lines), "totals": scoring.categories.totals_from_line(total)}
        if with_counted:
            side["counted_games"] = len(counted)
            side["counted_totals"] = scoring.categories.totals_from_line(StatLine.sum(counted))
        return side
    side = {"games": len(lines), "points": round(sum(scoring.points.score(l) for l in lines), 1)}
    if with_counted:
        side["counted_games"] = len(counted)
        side["counted_points"] = round(sum(scoring.points.score(l) for l in counted), 1)
    return side


def score_window(move: Move, span: tuple[date, date], stats_through: Optional[date], *,
                 add_nba: Optional[int], drop_nba: Optional[int], games: Games,
                 active_days: set[date], scoring: ResolvedScoring) -> dict[str, Any]:
    """One window's result: each side's games, and the add's games in an active slot here."""
    start, end = span

    def lines(nba_id: Optional[int]) -> list[tuple[date, StatLine]]:
        if nba_id is None:
            return []
        return [(d, line) for d, line in games.get(nba_id, ()) if start <= d <= end]

    out: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "complete": stats_through is not None and end <= stats_through,
        "add": None,
        "drop": None,
    }
    if move.add is not None:
        played = lines(add_nba)
        out["add"] = _side([l for _, l in played], [l for d, l in played if d in active_days], scoring, True)
    if move.drop is not None:
        out["drop"] = _side([l for _, l in lines(drop_nba)], [], scoring, False)

    if scoring.is_categories and scoring.categories is not None:
        if out["add"] is not None and out["drop"] is not None:
            cmp = scoring.categories.compare(out["add"]["totals"], out["drop"]["totals"])
            out["edge"] = {
                "won": [i.key for i in cmp.items if i.winner == "you"],
                "lost": [i.key for i in cmp.items if i.winner == "opp"],
                "tied": [i.key for i in cmp.items if i.winner == "tie"],
            }
    else:
        gained = out["add"]["points"] if out["add"] is not None else 0.0
        lost = out["drop"]["points"] if out["drop"] is not None else 0.0
        out["net"] = round(gained - lost, 1)
    return out


def held_through(move: Move, days: list[DayRoster]) -> Optional[str]:
    """The last stored day the added player was still on the team, counting on from the
    day he arrived (None for a drop)."""
    if move.add is None:
        return None
    last = move.day.nba_date
    for day in days:
        if day.scoring_period_id <= move.day.scoring_period_id:
            continue
        if move.add[0] not in day.players:
            break
        last = day.nba_date
    return last.isoformat()


def build_rows(league: League, moves: list[Move], *, games: Games, nba_ids: dict[int, int],
               scoring: ResolvedScoring, stats_through: Optional[date],
               matchup_end: Callable[[DayRoster], Optional[date]]) -> list[dict[str, Any]]:
    """The ledger's rows for one league, ready to insert."""
    team_days: dict[int, list[DayRoster]] = defaultdict(list)
    for day in league.days:
        team_days[day.provider_team_id].append(day)
    for days in team_days.values():
        days.sort(key=lambda d: d.scoring_period_id)

    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for move in moves:
        day = move.day
        days = team_days[day.provider_team_id]
        add_nba = nba_ids.get(move.add[0]) if move.add else None
        drop_nba = nba_ids.get(move.drop[0]) if move.drop else None
        active_days = {
            d.nba_date for d in days
            if move.add is not None and move.add[0] in d.players and d.players[move.add[0]][1] in ACTIVE_SLOT_IDS
        }
        windows = {
            name: score_window(move, span, stats_through, add_nba=add_nba, drop_nba=drop_nba, games=games,
                               active_days=active_days, scoring=scoring)
            for name, span in windows_of(day.nba_date, matchup_end(day)).items()
        }
        windows["held_through"] = held_through(move, days)
        write = move.write
        rows.append({
            "provider": league.key.provider,
            "provider_league_id": league.key.provider_league_id,
            "season": league.key.season,
            "provider_team_id": day.provider_team_id,
            "team_name": day.team_name,
            "scoring_period_id": day.scoring_period_id,
            "nba_date": day.nba_date,
            "add_player_id": move.add[0] if move.add else None,
            "add_nba_id": add_nba,
            "add_name": move.add[1] if move.add else None,
            "drop_player_id": move.drop[0] if move.drop else None,
            "drop_nba_id": drop_nba,
            "drop_name": move.drop[1] if move.drop else None,
            "paired": move.paired,
            "source": move.source,
            "team_id": write.team_id if write else league.cv_teams.get(day.provider_team_id),
            "roster_move_id": write.roster_move_id if write else None,
            "proposal_id": write.proposal_id if write else None,
            "forecast": write.forecast if write else None,
            "reason": write.reason if write else None,
            "format": "categories" if scoring.is_categories else "points",
            "windows": windows,
            "net_points": None if scoring.is_categories else windows["two_weeks"]["net"],
            "updated_at": now,
        })
    return rows


# ------------------------------- loading ------------------------------- #


def active_leagues(today: date, days: int = ACTIVE_LEAGUE_DAYS) -> list[LeagueKey]:
    """Leagues with a stored snapshot in the last `days` days."""
    query = (
        LineupSnapshot.select(LineupSnapshot.provider, LineupSnapshot.provider_league_id, LineupSnapshot.season)
        .where(LineupSnapshot.nba_date >= today - timedelta(days=days))
        .distinct()
    )
    return sorted(LeagueKey(p, str(lid), int(s)) for p, lid, s in query.tuples())


def _league_rows(key: LeagueKey):
    return (
        (LineupSnapshot.provider == key.provider)
        & (LineupSnapshot.provider_league_id == key.provider_league_id)
        & (LineupSnapshot.season == key.season)
    )


def load_days(key: LeagueKey) -> list[DayRoster]:
    headers = list(LineupSnapshot.select().where(_league_rows(key)))
    players: dict[int, dict[int, tuple[str, int]]] = {h.id: {} for h in headers}
    if headers:
        query = (
            LineupSnapshotPlayer.select(LineupSnapshotPlayer.snapshot, LineupSnapshotPlayer.player_id,
                                        LineupSnapshotPlayer.player_name, LineupSnapshotPlayer.lineup_slot_id)
            .join(LineupSnapshot)
            .where(_league_rows(key))
        )
        for snapshot_id, player_id, name, slot_id in query.tuples():
            players[snapshot_id][int(player_id)] = (name, int(slot_id))
    return [
        DayRoster(
            provider_team_id=h.provider_team_id, team_name=h.team_name, scoring_period_id=h.scoring_period_id,
            nba_date=h.nba_date, matchup_period_id=h.matchup_period_id, players=players[h.id],
        )
        for h in headers
    ]


def _info(raw: str) -> dict[str, Any]:
    try:
        info = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def cv_teams_in(key: LeagueKey, days: list[DayRoster]) -> dict[int, int]:
    """The league's Court Vision teams, by provider team id: the id the team learned from
    ESPN, else the stored days' team of the same name. A provider team two users both
    saved goes to the lower usr.teams id."""
    newest_by_name: dict[str, int] = {}
    for day in sorted(days, key=lambda d: d.scoring_period_id):
        newest_by_name[day.team_name.strip()] = day.provider_team_id
    found: dict[int, int] = {}
    for team_id, raw in Team.select(Team.team_id, Team.league_info).order_by(Team.team_id).tuples():
        info = _info(raw)
        if (info.get("provider") or "espn") != key.provider:
            continue
        if str(info.get("league_id")) != key.provider_league_id or str(info.get("year")) != str(key.season):
            continue
        provider_team_id = info.get("espn_team_id")
        if provider_team_id is None:
            provider_team_id = newest_by_name.get(str(info.get("team_name") or "").strip())
        if provider_team_id is not None:
            found.setdefault(int(provider_team_id), team_id)
    return found


def _player_ids(moves: Any, action: str) -> frozenset[int]:
    out = set()
    for m in moves or []:
        if isinstance(m, dict) and m.get("action") == action and m.get("player_id") is not None:
            try:
                out.add(int(m["player_id"]))
            except (TypeError, ValueError):
                continue
    return frozenset(out)


def load_writes(cv_teams: dict[int, int], days: list[DayRoster]) -> list[CvWrite]:
    """Court Vision's add/drops on the league's teams that went through, each with who made
    it: a stream lane's pickup, a scheduled pickup, a Dime proposal the user sent (as a write
    or as a scheduled pickup), or the user's own from the desk.

    Only this season's: a saved team keeps its id (and its audit rows) when it rolls into a
    new season, and scoring periods restart at 1, so a write is kept only when its own date
    falls within `SEASON_MARGIN_DAYS` of the league's stored days."""
    provider_of = {team_id: provider_team_id for provider_team_id, team_id in cv_teams.items()}
    if not provider_of or not days:
        return []
    since = min(d.nba_date for d in days) - timedelta(days=SEASON_MARGIN_DAYS)
    until = max(d.nba_date for d in days) + timedelta(days=SEASON_MARGIN_DAYS)
    rows = list(
        RosterMove.select()
        .where((RosterMove.kind == "transaction") & RosterMove.status.in_(WRITTEN)
               & RosterMove.team.in_(list(provider_of)) & RosterMove.scoring_period_id.is_null(False)
               & RosterMove.nba_date.between(since, until))
    )
    if not rows:
        return []
    ids = [r.id for r in rows]
    pickups = {p.audit_id: p for p in ScheduledPickup.select().where(ScheduledPickup.audit_id.in_(ids))}
    proposals_by_audit: dict[int, Any] = {}
    proposals_by_pickup: dict[int, Any] = {}
    pickup_ids = [p.id for p in pickups.values()]
    where = AiProposal.audit_id.in_(ids)
    if pickup_ids:
        where = where | AiProposal.pickup_id.in_(pickup_ids)
    for p in AiProposal.select().where(where & (AiProposal.state == "sent")):
        if p.audit_id is not None:
            proposals_by_audit[p.audit_id] = p
        if p.pickup_id is not None:
            proposals_by_pickup[p.pickup_id] = p

    writes: list[CvWrite] = []
    for r in rows:
        pickup = pickups.get(r.id)
        proposal = proposals_by_audit.get(r.id) or (proposals_by_pickup.get(pickup.id) if pickup else None)
        if proposal is not None:
            source = "dime"
        elif pickup is not None:
            source = "lane" if pickup.lane_id is not None else "scheduled"
        elif r.source == "scheduled":
            source = "scheduled"
        else:
            source = "you"
        writes.append(CvWrite(
            roster_move_id=r.id, team_id=r.team_id, provider_team_id=provider_of[r.team_id],
            scoring_period_id=int(r.scoring_period_id), source=source,
            adds=_player_ids(r.moves, "add"), drops=_player_ids(r.moves, "drop"),
            proposal_id=proposal.id if proposal else None,
            forecast=proposal.projection if proposal else None,
            reason=proposal.reason if proposal else None,
        ))
    return writes


def load_games(nba_ids: Iterable[int], start: date, end: date) -> Games:
    ids = sorted(set(nba_ids))
    games: Games = defaultdict(list)
    if not ids:
        return games
    query = (
        PlayerGameStats.select()
        .where(PlayerGameStats.player.in_(ids) & PlayerGameStats.game_date.between(start, end)
               & PlayerGameStats.for_fantasy())
        .order_by(PlayerGameStats.game_date)
    )
    for row in query:
        games[row.player_id].append((row.game_date, StatLine.from_game_row(row)))
    return games


def stats_through(start: date) -> Optional[date]:
    """The last day with box scores, from `start` on (None before the first)."""
    from peewee import fn

    value = (
        PlayerGameStats.select(fn.MAX(PlayerGameStats.game_date))
        .where((PlayerGameStats.game_date >= start) & PlayerGameStats.for_fantasy())
        .scalar()
    )
    return value


def league_scoring(cv_teams: dict[int, int]) -> ResolvedScoring:
    """The league's own scoring, from the first saved team linked to it. Never a team's
    `scoring_preview`: the rows are shared by every team in the league, so one user's
    display override must not decide how everyone's moves are scored."""
    for team_id in sorted(cv_teams.values()):
        team = Team.get_or_none(Team.team_id == team_id)
        if team is not None and team.league_id is not None:
            return resolve_scoring(team.league)
    return resolve_scoring(None)


def matchup_end_resolver(cv_teams: dict[int, int]) -> Callable[[DayRoster], Optional[date]]:
    """A day's matchup's last day: the league's own matchup periods, else the calendar's week."""
    periods: Optional[dict] = None
    for team_id in sorted(cv_teams.values()):
        team = Team.get_or_none(Team.team_id == team_id)
        if team is not None and team.league_id is not None and team.league.matchup_periods:
            periods = team.league.matchup_periods
            break
    cache: dict[tuple[Optional[int], date], Optional[date]] = {}

    def resolve(day: DayRoster) -> Optional[date]:
        key = (day.matchup_period_id, day.nba_date)
        if key not in cache:
            end: Optional[date] = None
            try:
                if periods and day.matchup_period_id:
                    dates = schedule_service.get_espn_matchup_dates(periods, day.matchup_period_id)
                    if dates and dates[0] <= day.nba_date <= dates[1]:
                        end = dates[1]
                if end is None:
                    week = schedule_service.get_current_matchup(day.nba_date)
                    end = week["end_date"] if week else None
            except FileNotFoundError:
                end = None
            cache[key] = end
        return cache[key]

    return resolve


# ------------------------------- the rebuild ------------------------------- #


@dataclass(frozen=True)
class LeagueRebuild:
    key: LeagueKey
    days: int
    moves: int
    by_source: dict[str, int]


def replace_rows(key: LeagueKey, rows: list[dict[str, Any]]) -> None:
    with db.atomic():
        MoveOutcome.delete().where(
            (MoveOutcome.provider == key.provider)
            & (MoveOutcome.provider_league_id == key.provider_league_id)
            & (MoveOutcome.season == key.season)
        ).execute()
        for i in range(0, len(rows), 500):
            MoveOutcome.insert_many(rows[i:i + 500]).execute()


def rebuild_league(key: LeagueKey) -> LeagueRebuild:
    """Read the league's stored days, find and score every move, and replace its rows."""
    days = load_days(key)
    cv_teams = cv_teams_in(key, days)
    league = League(key=key, days=days, cv_teams=cv_teams, writes=load_writes(cv_teams, days))
    moves = attribute(detect_changes(days), league.writes)
    espn_ids = {p[0] for m in moves for p in (m.add, m.drop) if p is not None}
    nba_ids = nba_ids_by_espn_id(sorted(espn_ids))
    rows: list[dict[str, Any]] = []
    if moves:
        first = min(m.day.nba_date for m in moves)
        last = max(m.day.nba_date for m in moves) + timedelta(days=TWO_WEEKS + 7)
        games = load_games(nba_ids.values(), first, last)
        rows = build_rows(league, moves, games=games, nba_ids=nba_ids, scoring=league_scoring(cv_teams),
                          stats_through=stats_through(first), matchup_end=matchup_end_resolver(cv_teams))
    replace_rows(key, rows)
    by_source: dict[str, int] = defaultdict(int)
    for m in moves:
        by_source[m.source] += 1
    log.info("move_ledger_league_rebuilt", provider=key.provider, league=key.provider_league_id,
             season=key.season, days=len(days), moves=len(moves), by_source=dict(by_source))
    return LeagueRebuild(key=key, days=len(days), moves=len(moves), by_source=dict(by_source))


# ------------------------------- reading the ledger ------------------------------- #


SCOPES = ("team", "league")


def league_key_of(info: Any) -> LeagueKey:
    provider = info.provider.value if hasattr(info.provider, "value") else str(info.provider)
    return LeagueKey(provider=provider, provider_league_id=str(info.league_id), season=int(info.year))


def own_provider_team_id(key: LeagueKey, info: Any) -> Optional[int]:
    """The team's id inside its league: the one it learned from ESPN, else the newest stored
    day's team of the same name."""
    if getattr(info, "espn_team_id", None) is not None:
        return int(info.espn_team_id)
    row = (
        LineupSnapshot.select(LineupSnapshot.provider_team_id)
        .where(_league_rows(key) & (LineupSnapshot.team_name == (info.team_name or "").strip()))
        .order_by(LineupSnapshot.scoring_period_id.desc())
        .first()
    )
    return row.provider_team_id if row else None


@dataclass(frozen=True)
class OutcomeQuery:
    team_id: int
    key: LeagueKey
    provider_team_id: Optional[int]
    scope: str = "team"                          # team | league
    sources: tuple[str, ...] = ()
    nba_player_id: Optional[int] = None
    since: Optional[date] = None
    limit: int = 20


def select_outcomes(q: OutcomeQuery) -> list[MoveOutcome]:
    """Every ledger row the query matches, newest first."""
    where = (
        (MoveOutcome.provider == q.key.provider)
        & (MoveOutcome.provider_league_id == q.key.provider_league_id)
        & (MoveOutcome.season == q.key.season)
    )
    from peewee import fn

    mine = fn.COALESCE(MoveOutcome.team_id, 0) == q.team_id
    if q.provider_team_id is not None:
        mine = mine | (MoveOutcome.provider_team_id == q.provider_team_id)
    if q.scope == "team":
        where = where & mine
    if q.sources:
        # By the source the asking team sees (`visible_source`): a rival's move is outside
        chosen = mine & MoveOutcome.source.in_(list(q.sources))
        where = where & ((chosen | ~mine) if "outside" in q.sources else chosen)
    if q.nba_player_id is not None:
        where = where & ((MoveOutcome.add_nba_id == q.nba_player_id) | (MoveOutcome.drop_nba_id == q.nba_player_id))
    if q.since is not None:
        where = where & (MoveOutcome.nba_date >= q.since)
    return list(MoveOutcome.select().where(where).order_by(MoveOutcome.nba_date.desc(), MoveOutcome.id.desc()))


def _player(nba_id: Optional[int], name: Optional[str]) -> Optional[dict[str, Any]]:
    if name is None and nba_id is None:
        return None
    return {"player_id": nba_id, "name": name}


def is_mine(row: Any, q: OutcomeQuery) -> bool:
    return row.team_id_id == q.team_id or (q.provider_team_id is not None and row.provider_team_id == q.provider_team_id)


def visible_source(row: Any, q: OutcomeQuery) -> str:
    """Who made the move, as the asking team may know it. A rival can use Court Vision too,
    and how they made a move (a lane, their own Dime, the desk) is theirs: to everyone else
    in the league it was made outside Court Vision, which is all ESPN shows them as well."""
    return row.source if is_mine(row, q) else "outside"


def outcome_view(row: Any, q: OutcomeQuery) -> dict[str, Any]:
    """One move as the agent and the desk read it. Player ids are NBA ids. A rival's move
    never carries its Court Vision source, forecast or reason (`visible_source`)."""
    mine = is_mine(row, q)
    view: dict[str, Any] = {
        "move_id": row.id,
        "date": row.nba_date.isoformat() if hasattr(row.nba_date, "isoformat") else str(row.nba_date),
        "team_name": row.team_name,
        "mine": mine,
        "source": visible_source(row, q),
        "add": _player(row.add_nba_id, row.add_name),
        "drop": _player(row.drop_nba_id, row.drop_name),
        "paired": bool(row.paired),
        "windows": row.windows,
    }
    if mine and (row.forecast is not None or row.reason):
        view["forecast"] = row.forecast
        view["reason"] = row.reason
    return view


def summarize(rows: Iterable[Any], fmt: str, q: Optional[OutcomeQuery] = None) -> list[dict[str, Any]]:
    """Per source: how many moves, how many have a complete two weeks, and how those went
    (points: the summed net and how many came out ahead or behind; categories: the
    categories the adds won and lost against the drops). With `q`, rivals' moves count
    as outside (`visible_source`)."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        groups[visible_source(row, q) if q is not None else row.source].append(row)
    out: list[dict[str, Any]] = []
    for source in sorted(groups, key=lambda s: SOURCE_ORDER.get(s, 99)):
        items = groups[source]
        settled = [r for r in items if ((r.windows or {}).get("two_weeks") or {}).get("complete")]
        entry: dict[str, Any] = {"source": source, "moves": len(items), "settled": len(settled)}
        if fmt == "categories":
            won = lost = 0
            for r in settled:
                edge = r.windows["two_weeks"].get("edge") or {}
                won += len(edge.get("won") or [])
                lost += len(edge.get("lost") or [])
            entry.update(categories_won=won, categories_lost=lost)
        else:
            nets = [float(r.windows["two_weeks"].get("net") or 0) for r in settled]
            entry.update(
                net_points=round(sum(nets), 1),
                ahead=sum(1 for n in nets if n > 0),
                behind=sum(1 for n in nets if n < 0),
            )
        out.append(entry)
    return out


SOURCE_ORDER = {"dime": 0, "lane": 1, "scheduled": 2, "you": 3, "outside": 4}


def read_outcomes(q: OutcomeQuery) -> dict[str, Any]:
    """The query's moves (up to its limit) and the per-source summary over all it matched."""
    rows = select_outcomes(q)
    fmt = rows[0].format if rows else league_scoring({0: q.team_id}).format
    return {
        "format": fmt,
        "moves": [outcome_view(r, q) for r in rows[: q.limit]],
        "total": len(rows),
        "summary": summarize(rows, fmt, q),
    }
