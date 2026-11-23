"""
Protected players: the roster lock (usr.roster_protections, migration 0048; the "Roster lock"
initiative doc).

    list       GET    /teams/{id}/protections               the team's protected players
    protect    PUT    /teams/{id}/protections/{player_id}   protect one, cancelling what would drop him
    unprotect  DELETE /teams/{id}/protections/{player_id}   unprotect one

A protected player can't leave the roster through Court Vision, and nothing in it suggests
dropping him, until the user unprotects him. Only drops: lineup (slot) moves still move him.

Where it is enforced. Every add/drop Court Vision sends -- the Week desk's, a scheduled
pickup's, a stream lane's, Dime's chips and autopilot -- runs through the roster
transaction's write chain (`RosterTransactionService._write`), which refuses a protected
drop before anything is audited or sent (`guard`). That is the guarantee, and it fails
closed: a protection that can't be read refuses the drop. The earlier refusals (the desk's
routes, the pickup executor, a lane's start, Dime's `propose_move`) are there to give the
reason sooner and in words, and the suggestion filters (the daily swap's drop candidates,
Dime's what-ifs and stream plans, the market) read `ids_soft`, which fails soft.

Dime is told up front rather than refused: every turn's header lists each team's protected
players (`for_teams`), its board reads mark them, its planning tools pass over them with a
note, and its prompt says a protected player is never a drop. The chip refusal is the
backstop, worded for Dime (`dime_words`).

Protecting cancels what would drop him (James, 2026-11-15): the open stream lanes holding his
spot are stopped, and the pending pickups dropping him are cancelled. The row is stored first,
so an attempt already under way meets the check before ESPN; one already sending is named in
the answer (`not_cancelled`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from peewee import IntegrityError

from core.errors import AppError, NotFoundError
from core.logging import get_logger
from db.base import run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.roster_protection import (
    ProtectedPlayer,
    ProtectionCancelled,
    RosterProtectionsData,
    RosterProtectionsResp,
)

log = get_logger("roster_protection")

# The refusal reason every check gives (RosterTransactionInvalid.data.reason and the pickup's)
DROP_PROTECTED = "drop_protected"
# Why a stream lane ended when its spot's player was protected (the lane's event reason)
LANE_STOPPED = "stopped_for_protected"


@dataclass(frozen=True)
class Protected:
    player_id: int                      # ESPN
    nba_player_id: Optional[int]
    name: str
    team: Optional[str]


def protected_of(row: Any) -> Protected:
    return Protected(player_id=row.player_id, nba_player_id=row.nba_player_id, name=row.player_name,
                     team=row.player_team)


def refusal_words(name: str) -> str:
    """The sentence a refused drop gives the user."""
    return f"{name} is protected: unprotect him in the Week desk to drop him"


def dime_words(name: str) -> str:
    """The sentence a refused drop gives Dime: what to do instead, not only why."""
    return (f"{name} is protected by the user, so he is never a drop: choose another player to drop (or none, "
            "with an open seat), and don't suggest dropping him")


def dime_lane_words(name: str) -> str:
    """The sentence a lane on a protected player's spot gives Dime (a lane drops its spot's player)."""
    return (f"{name} is protected by the user, so he is never a drop and a stream lane can't take his spot: name "
            "another player's spot, or let the drop rule choose")


# What Dime's reads say beside a player marked `protected` (its board, its rosters, its plans)
DIME_NOTE = ("A player marked protected was protected by the user: he is never a drop, so a move that needs a "
             "drop takes another player, and moving him between slots is fine.")


# ------------------------------- the repository ------------------------------- #


def _model():
    from db.models.roster_protections import RosterProtection
    return RosterProtection


def _ids(team_id: int) -> frozenset[int]:
    Row = _model()
    return frozenset(r.player_id for r in Row.select(Row.player_id).where(Row.team_id == team_id))


def _rows(team_id: int) -> list:
    Row = _model()
    return list(Row.select().where(Row.team_id == team_id).order_by(Row.player_name, Row.id))


def _rows_for_teams(team_ids: Sequence[int]) -> list:
    Row = _model()
    return list(Row.select().where(Row.team_id.in_(list(team_ids))).order_by(Row.team_id, Row.player_name, Row.id))


def _insert(team_id: int, user_id: int, player_id: int, nba_player_id: Optional[int], name: str,
            team: Optional[str]) -> tuple[Any, bool]:
    """(the row, whether this call made it). Protecting one already protected changes nothing."""
    Row = _model()
    existing = Row.get_or_none((Row.team_id == team_id) & (Row.player_id == player_id))
    if existing is not None:
        return existing, False
    try:
        row = Row.create(team_id=team_id, user_id=user_id, player_id=player_id, nba_player_id=nba_player_id,
                         player_name=name, player_team=team, created_at=datetime.now(timezone.utc))
        return row, True
    except IntegrityError:
        # The same protect sent twice at once: the other one stored it
        return Row.get((Row.team_id == team_id) & (Row.player_id == player_id)), False


def _delete(team_id: int, player_id: int) -> Optional[Any]:
    Row = _model()
    row = Row.get_or_none((Row.team_id == team_id) & (Row.player_id == player_id))
    if row is None:
        return None
    Row.delete().where(Row.id == row.id).execute()
    return row


def _pickups_dropping(team_id: int, player_id: int) -> list:
    """The team's pending scheduled pickups that drop this player and belong to no lane (a lane's
    pickup goes with its lane's stop)."""
    from db.models.scheduled_pickups import ScheduledPickup as Pickup
    return list(Pickup.select().where((Pickup.team == team_id) & (Pickup.status == "pending")
                                      & (Pickup.drop_player_id == player_id) & Pickup.lane_id.is_null(True))
                .order_by(Pickup.nba_date, Pickup.id))


def _lanes_holding(team_id: int, player_id: int) -> list:
    """The team's open stream lanes (active or paused) whose spot he holds: each would drop him."""
    from db.models.stream_lanes import StreamLane as Lane
    return list(Lane.select().where((Lane.team == team_id) & (Lane.status.in_(("active", "paused")))
                                    & (Lane.holder_player_id == player_id)).order_by(Lane.id))


# ------------------------------- the reads ------------------------------- #


async def ids(team_id: int) -> frozenset[int]:
    """The team's protected players (ESPN ids). Raises when they can't be read: a write must not
    drop a player whose protection it couldn't check."""
    return await run_db("protections.ids", _ids, team_id)


async def ids_soft(team_id: int) -> frozenset[int]:
    """The team's protected players for a suggestion: a failed read is none, logged, so a lookup
    never fails a suggestion it only narrows (the writes check again with `ids`)."""
    try:
        return await ids(team_id)
    except Exception as exc:
        log.warning("roster_protections_read_failed", team_id=team_id, error=str(exc))
        return frozenset()


async def for_team(team_id: int) -> list[Protected]:
    """The team's protected players, by name. Fails soft, as `ids_soft`."""
    try:
        return [protected_of(r) for r in await run_db("protections.rows", _rows, team_id)]
    except Exception as exc:
        log.warning("roster_protections_read_failed", team_id=team_id, error=str(exc))
        return []


async def for_teams(team_ids: Iterable[int]) -> dict[int, list[Protected]]:
    """Each team's protected players, by name (Dime's header). Fails soft."""
    wanted = sorted(set(team_ids))
    if not wanted:
        return {}
    try:
        rows = await run_db("protections.rows_for_teams", _rows_for_teams, wanted)
    except Exception as exc:
        log.warning("roster_protections_read_failed", team_ids=wanted, error=str(exc))
        return {}
    out: dict[int, list[Protected]] = {}
    for r in rows:
        out.setdefault(r.team_id_id, []).append(protected_of(r))
    return out


async def guard(team_id: int, drop: Optional[int], name: Optional[str] = None) -> None:
    """The check before ESPN (`RosterTransactionService._write`): a protected drop is refused,
    and an unreadable protection refuses too."""
    if drop is None:
        return
    if drop in await ids(team_id):
        from services.roster_transaction_service import _invalid
        raise _invalid(DROP_PROTECTED, refusal_words(name or "That player"), drop)


# ------------------------------- the routes ------------------------------- #


def _player(row: Any) -> ProtectedPlayer:
    return ProtectedPlayer(espn_id=row.player_id, nba_player_id=row.nba_player_id, name=row.player_name,
                           team=row.player_team, protected_at=row.created_at)


def _day(d) -> str:
    return f"{d:%a}"


class RosterProtectionService:

    @staticmethod
    async def _resp(team_id: int, message: str, *, cancelled: Sequence[ProtectionCancelled] = (),
                    not_cancelled: Sequence[str] = ()) -> RosterProtectionsResp:
        rows = await run_db("protections.rows", _rows, team_id)
        return RosterProtectionsResp(status=ApiStatus.SUCCESS, message=message, data=RosterProtectionsData(
            players=[_player(r) for r in rows], cancelled=list(cancelled), not_cancelled=list(not_cancelled)))

    @staticmethod
    async def list_for_team(team) -> RosterProtectionsResp:
        rows = await run_db("protections.rows", _rows, team.team_id)
        return await RosterProtectionService._resp(team.team_id, f"{len(rows)} protected player(s)")

    @staticmethod
    async def protect(team, league_info: LeagueInfo, player_id: int) -> RosterProtectionsResp:
        """Protect one of the team's players: he must be on the roster (today's, or tomorrow's after
        today's first tip, where a player added tonight already is). Then what would drop him goes:
        the lanes holding his spot are stopped and the pickups dropping him are cancelled."""
        from services.lineup_editor_service import _roster_slots
        from services.providers import get_provider_adapter
        from services.roster_transaction_service import next_day_period, _now

        adapter = get_provider_adapter(league_info.provider)
        slots = _roster_slots(team)
        state = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots)
        player = next((p for p in state.players if p.player_id == player_id), None)
        if player is None:
            tomorrow = next_day_period(state, _now())
            if tomorrow is not None:
                try:
                    later = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots,
                                                      scoring_period_id=tomorrow)
                    player = next((p for p in later.players if p.player_id == player_id), None)
                except Exception as exc:
                    log.warning("roster_protection_tomorrow_read_failed", team_id=team.team_id, error=str(exc))
        if player is None:
            raise AppError("PROTECTION_NOT_ON_ROSTER", "That player is not on your roster", status_code=422,
                           data={"player_id": player_id}, log_level="info")

        row, made = await run_db("protections.insert", _insert, team.team_id, team.user_id, player_id,
                                 player.nba_player_id, player.name, player.team or None)
        cancelled, not_cancelled = await RosterProtectionService._cancel_drops(team, player_id, player.name)
        log.info("roster_protection_set", team_id=team.team_id, player_id=player_id, new=made,
                 cancelled=[c.summary for c in cancelled], not_cancelled=not_cancelled)
        parts = [f"Protected {player.name}"]
        if cancelled:
            parts.append("cancelled " + " and ".join(c.summary for c in cancelled))
        if not_cancelled:
            parts.append(" and ".join(not_cancelled) + " was already being sent")
        return await RosterProtectionService._resp(team.team_id, "; ".join(parts), cancelled=cancelled,
                                                   not_cancelled=not_cancelled)

    @staticmethod
    async def _cancel_drops(team, player_id: int, name: str) -> tuple[list[ProtectionCancelled], list[str]]:
        from services.scheduled_pickup_service import IN_PROGRESS, PENDING, _cancel_pickup
        from services.stream_lane_service import StreamLaneBusy, StreamLaneEnded, StreamLaneService

        now = datetime.now(timezone.utc)
        cancelled: list[ProtectionCancelled] = []
        busy: list[str] = []
        for lane in await run_db("protections.lanes", _lanes_holding, team.team_id, player_id):
            try:
                await StreamLaneService.stop(team, lane.id, why=dict(reason=LANE_STOPPED, detail=None))
                cancelled.append(ProtectionCancelled(kind="lane", id=lane.id,
                                                     summary=f"the stream lane on {name}'s spot"))
            except StreamLaneBusy:
                # Its pickup is being made: the check before ESPN refuses it unless it was out already,
                # and the lane's next decision ends the lane (`StreamLaneService._decide_team`).
                busy.append(f"the stream lane's pickup dropping {name}")
            except StreamLaneEnded:
                pass
        for pickup in await run_db("protections.pickups", _pickups_dropping, team.team_id, player_id):
            found = await run_db("pickup.cancel", _cancel_pickup, team.team_id, pickup.id, now)
            if found is None:
                continue
            prior, _row = found
            if prior == PENDING:
                cancelled.append(ProtectionCancelled(kind="pickup", id=pickup.id,
                                                     summary=f"{_day(pickup.nba_date)}'s pickup of {pickup.add_name}"))
            elif prior == IN_PROGRESS:
                busy.append(f"{_day(pickup.nba_date)}'s pickup of {pickup.add_name}")
        return cancelled, busy

    @staticmethod
    async def unprotect(team, player_id: int) -> RosterProtectionsResp:
        row = await run_db("protections.delete", _delete, team.team_id, player_id)
        if row is None:
            raise NotFoundError("PROTECTION_NOT_FOUND", "That player isn't protected")
        log.info("roster_protection_cleared", team_id=team.team_id, player_id=player_id)
        return await RosterProtectionService._resp(team.team_id, f"{row.player_name} is no longer protected")


# ------------------------------- the cutover seed ------------------------------- #
#
# Dime's own rule never to drop one of the roster's top six by value gave way to these
# protections (James, 2026-11-15), so that the user decides who is never dropped. So that the
# cutover drops nothing Dime used to keep, a team Dime acts on gets its first draft picks still
# on the roster protected, once, when a check-in starts there (services/ai/checkins.py) and
# usr.autopilot.protections_seeded_at (0050) is still empty. The first picks by pick number,
# an auction's by price; a league with no draft on ESPN takes the roster's top by value, the
# rule this replaces. A player a pending pickup drops or an open lane's spot holds is left
# alone: the user is already moving him out.

SEED_DRAFT, SEED_VALUE = "draft", "value"


def draft_core(payload: dict, espn_team_id: Optional[int], n: Optional[int] = None) -> list[int]:
    """The team's draft picks (ESPN ids), the first `n` or all of them: by pick number, or by
    price in an auction. Empty when ESPN holds no completed draft for the league (or the team
    isn't known)."""
    if espn_team_id is None or (n is not None and n <= 0):
        return []
    picks = [p for p in ((payload.get("draftDetail") or {}).get("picks") or [])
             if int(p.get("playerId") or -1) > 0 and int(p.get("teamId") or -1) == espn_team_id]
    draft = ((payload.get("settings") or {}).get("draftSettings") or {})
    if str(draft.get("type") or "").upper() == "AUCTION":
        picks.sort(key=lambda p: (-(p.get("bidAmount") or 0), int(p.get("overallPickNumber") or 0)))
    else:
        picks.sort(key=lambda p: int(p.get("overallPickNumber") or 0))
    return [int(p["playerId"]) for p in (picks if n is None else picks[:n])]


def _moving_out(team_id: int) -> frozenset[int]:
    """The players the team's pending pickups drop or its open lanes' spots hold (ESPN ids)."""
    from db.models.scheduled_pickups import ScheduledPickup as Pickup
    from db.models.stream_lanes import StreamLane as Lane
    drops = {r.drop_player_id for r in Pickup.select(Pickup.drop_player_id).where(
        (Pickup.team == team_id) & (Pickup.status == "pending") & Pickup.drop_player_id.is_null(False))}
    holders = {r.holder_player_id for r in Lane.select(Lane.holder_player_id).where(
        (Lane.team == team_id) & (Lane.status.in_(("active", "paused"))) & Lane.holder_player_id.is_null(False))}
    return frozenset(drops | holders)


def _store_seed(team_id: int, user_id: int, players: Sequence[Any], now: datetime) -> list[str]:
    """Protect `players` and mark the team seeded, in one transaction; the names newly protected.
    A team another check-in seeded first is left as that one left it."""
    from db.base import db
    from db.models.autopilot import Autopilot
    with db.atomic():
        marked = (Autopilot.update(protections_seeded_at=now)
                  .where((Autopilot.team_id == team_id) & Autopilot.protections_seeded_at.is_null(True)).execute())
        if not marked:
            return []
        Row, held, made = _model(), _ids(team_id), []
        for p in players:
            if p.player_id in held:
                continue
            try:
                with db.atomic():       # a savepoint: a protect made at the same moment fails only this insert
                    Row.create(team_id=team_id, user_id=user_id, player_id=p.player_id, nba_player_id=p.nba_player_id,
                               player_name=p.name, player_team=p.team or None, created_at=now)
                made.append(p.name)
            except IntegrityError:
                pass
        return made


async def seed(team, league_info: LeagueInfo, *, n: Optional[int] = None,
               now: Optional[datetime] = None) -> list[str]:
    """Protect the team's first `n` draft picks still on its roster (AUTOPILOT_SEED_PROTECTED),
    once, and return the names newly protected. The picks are taken in order past those no longer
    rostered or being moved out, so up to `n` are protected. Raises when ESPN can't be read, so the
    next check-in tries again; a team not on ESPN (Dime moves no one there) is marked with none."""
    from core.settings import settings
    from schemas.common import FantasyProvider
    from services.lineup_editor_service import _roster_slots
    from services.providers import get_provider_adapter
    from services.providers.http import provider_get
    from services.espn_service import EspnService
    from services.roster_transaction_service import next_day_period
    from utils.constants import ESPN_FANTASY_ENDPOINT

    n = settings.autopilot_seed_protected if n is None else n
    now = now or datetime.now(timezone.utc)
    if league_info.provider != FantasyProvider.ESPN or n <= 0:
        return await run_db("protections.seed", _store_seed, team.team_id, team.user_id, [], now)
    adapter, slots = get_provider_adapter(league_info.provider), _roster_slots(team)
    board = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots)
    payload = await provider_get(
        "espn", ESPN_FANTASY_ENDPOINT.format(int(league_info.year), int(league_info.league_id)),
        params={"view": ["mDraftDetail", "mSettings"]}, cookies=EspnService._cookies(league_info),
        expect_key="settings")
    rostered = {p.player_id: p for p in board.players}
    order = draft_core(payload, board.espn_team_id or league_info.espn_team_id)
    source = SEED_DRAFT if order else SEED_VALUE
    if order and any(pid not in rostered for pid in order):
        # After today's first tip a player added tonight is on tomorrow's board only (as `protect`)
        tomorrow = next_day_period(board, now)
        if tomorrow is not None:
            later = await adapter.read_lineup(team.team_id, league_info, fallback_slot_counts=slots,
                                              scoring_period_id=tomorrow)
            for p in later.players:
                rostered.setdefault(p.player_id, p)
    if not order:
        order = [p.player_id for p in sorted(board.players, key=lambda p: p.avg_points or 0.0, reverse=True)]
    moving = await run_db("protections.moving_out", _moving_out, team.team_id)
    chosen = [rostered[pid] for pid in order if pid in rostered and pid not in moving][:n]
    made = await run_db("protections.seed", _store_seed, team.team_id, team.user_id, chosen, now)
    log.info("roster_protection_seeded", team_id=team.team_id, source=source, protected=made,
             skipped=[rostered[pid].name for pid in order if pid in moving and pid in rostered])
    return made


async def seed_for(team_id: int, user_id: int) -> list[str]:
    """`seed` for a team by id, with its owner's league credentials (a check-in's start)."""
    from api.deps import ensure_team_owned, load_owned_league_info
    team = await run_db("ai.owned_team", ensure_team_owned, team_id, user_id)
    if team is None:
        return []
    return await seed(team, await load_owned_league_info(team))
