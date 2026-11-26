"""
Daily actions at Act (the Daily actions row of the automations table at
/account?v=automation): once a day before the first tip-off, data-platform's
daily_actions pipeline posts each team at Act here
(POST /v1/internal/jobs/daily-actions/run), and Court Vision makes what the Week
desk's list says (services/daily_actions_service.py), of two kinds only:

1. IR moves (`ir_in`, `ir_out` rows): an injured player who sits today into IR's
   open seat, a healthy one off IR into an open roster spot, all of one read in one
   write through the lineup chain. One off IR with the roster full stays the user's.
2. Open roster spots (`add` rows): the list's pickup when a spot is open, the day's
   best free agent with a game today, through the add/drop chain.

Both go out as source 'daily_actions'. The list is read again after every write, so
a spot an IR move opened is filled, and an IR seat a healthy player left is taken,
up to MAX_WRITES writes a pass.

It never drops anyone: the day's drop-and-add swap (`add_drop`) stays the user's
(and Dime's at AI), since a drop can't be taken back. It never sets slots either: a
pickup lands where ESPN puts him (the bench), and the Lineup row's fill seats him
(auto-lineup runs after this pass on the same pre-game tick).

A pickup is held to:
- the league's add limit (`budget_block`, with the team's pickups still to be made),
- the spots the team's add-only scheduled pickups need: each needs an open spot on
  its day, so that many spots stay open,
- the day: the board's ESPN day must be the pipeline's, before its first tip-off (a
  later add is next-day business on ESPN, and the list is about tonight).

A team at AI is Dime's (its own adds and drops answer the list), so it is skipped,
as is one whose level is not act, one on a provider without the writes, or any
team while ROSTER_WRITES_ENABLED is off.

Every outcome comes back, never raised: applied (at least one write went;
`ir_moves`, `adds`), noop (nothing of these kinds on the list), skipped (with why),
rejected (ESPN refused the first write, so nothing went), failed (the board or the
writer couldn't be reached first, or a spot is open and the free-agent pool couldn't be
read, so the next poll tries again). Once a write has gone, a later refusal or outage
ends the pass as applied with `held` saying what didn't go.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Optional, Sequence

import pytz

from core.errors import ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import ApiStatus
from schemas.daily_actions import (
    DailyAction,
    DailyActionsAdd,
    DailyActionsData,
    DailyActionsRunData,
    DailyActionsRunReq,
    DailyActionsRunResp,
)
from schemas.lineup_editor import LineupMoveResult, LineupState
from services.acquisitions import budget_block
from services.ai import autonomy, levels
from services.daily_actions_service import DailyActionsService, open_spots
from services.lineup_editor_service import (
    LineupEditorService,
    RosterWriteRejected,
    RosterWriteUnavailable,
    _roster_slots,
)
from services.lineup_planner import Move
from services.providers import get_provider_adapter
from services.roster_transaction_service import RosterTransactionInvalid, RosterTransactionService, _moves_json

log = get_logger("daily_actions_job")

SOURCE = "daily_actions"
EASTERN = pytz.timezone("US/Eastern")
IR_KINDS = ("ir_in", "ir_out")
# Writes a pass may make: IR moves a read at a time, then a pickup per open spot. Two IR reads
# (a healthy player off a full IR, then an injured one into the seat he left) and two pickups
# cover a real day; the cap keeps a board that never settles from looping.
MAX_WRITES = 4
# What stops the pass once the first game has tipped: a move sent after it is next-day business
TIPPED_HELD = "The first game tipped off, so nothing more was sent"
# The list has no pickup because the free-agent pool couldn't be read this time (the free-agent
# call failed, or it answered for another day), not because no free agent fits: a later poll can
# read it. A pool scored another way than the board never fits, so it is no reason to retry.
POOL_HELD = "The free-agent list couldn't be read, so an open spot stays open"
POOL_FOR_GOOD = ("value_kind_mismatch",)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------- pure ------------------------------- #


@dataclass(frozen=True)
class Pending:
    """The team's pickups still to be made: the days each will take one of the league's adds, and
    the add-only ones (no drop), each needing an open roster spot on its day."""

    days: tuple[date, ...] = ()
    add_only: tuple[tuple[str, date], ...] = ()      # (player's name, day)


@dataclass(frozen=True)
class Step:
    """What the pass does next with the list it just read: `ir` (write these rows' moves), `add`
    (pick up this row's free agent), `held` (a spot stays open: `why`), `unread` (a spot is open and
    free to fill, but the free-agent pool couldn't be read: `why`) or `done`."""

    kind: str
    rows: tuple[DailyAction, ...] = ()
    why: Optional[str] = None


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def tipped(state: LineupState, now: datetime) -> bool:
    """The board's day has passed its first tip-off (no tip known: not tipped)."""
    if not state.nba_date or not state.first_game_time_et:
        return False
    tip = EASTERN.localize(datetime.combine(date.fromisoformat(state.nba_date),
                                            time.fromisoformat(state.first_game_time_et)))
    return now >= tip


def next_step(data: DailyActionsData, pending: Pending, *, adds_left: bool = True) -> Step:
    """The list's next move of the kinds the pass makes. IR first: a healthy player back from IR
    takes an open spot before a free agent does, and an injured one into IR opens one. Then the
    pickup for an open spot, unless the league's adds are used up or a scheduled add-only pickup
    needs the spot. `adds_left` False: the pass can make no more writes for a pickup."""
    ir = tuple(a for a in data.actions if a.kind in IR_KINDS and a.moves and a.blocked_reason is None)
    if ir:
        return Step("ir", ir)
    add = next((a for a in data.actions if a.kind == "add" and a.transaction is not None), None)
    state = data.lineup
    unread = (add is None and state is not None and open_spots(state) > 0
              and bool(data.streamers_error) and data.streamers_error not in POOL_FOR_GOOD)
    if add is None and not unread:
        return Step("done")
    if state is not None and pending.add_only and open_spots(state) <= len(pending.add_only):
        name, day = min(pending.add_only, key=lambda p: p[1])
        return Step("held", why=f"The open spot is kept for the pickup you scheduled: {name} on {_day(day)}")
    if state is not None and state.nba_date:
        blocked = budget_block(state.acquisitions, date.fromisoformat(state.nba_date), pending=pending.days)
        if blocked is not None:
            return Step("held", why=f"An open spot stays open: {blocked}")
    if unread:
        return Step("unread", why=POOL_HELD)
    if not adds_left:
        return Step("held", why="An open spot stays open: the pass made as many moves as it makes in a day")
    return Step("add", (add,))


def _moves_of(rows: Sequence[DailyAction]) -> list[Move]:
    return [Move(m.player_id, m.from_slot_id, m.to_slot_id, role=m.role, note=m.note) for a in rows for m in a.moves]


def _results_of(rows: Sequence[DailyAction]) -> list[LineupMoveResult]:
    return [m for a in rows for m in a.moves]


# ------------------------------- DB (run in the executor) ------------------------------- #


def _autopilot(team_id: int) -> Any:
    from db.models.autopilot import Autopilot
    return Autopilot.get_or_none(Autopilot.team_id == team_id)


def _pending(team_id: int) -> Pending:
    from db.models.scheduled_pickups import ScheduledPickup as Row
    rows = list(Row.select(Row.nba_date, Row.drop_player_id, Row.add_name)
                .where((Row.team == team_id) & (Row.status == "pending")))
    return Pending(days=tuple(r.nba_date for r in rows),
                   add_only=tuple((r.add_name, r.nba_date) for r in rows if r.drop_player_id is None))


async def _level_of(team_id: int, user_id: int) -> tuple[str, bool]:
    """The team's Daily actions level below AI, and whether the row is at AI on it."""
    prefs, overrides = await levels.prefs_of(user_id, [team_id])
    pilot = await run_db("daily_actions_job.autopilot", _autopilot, team_id)
    return levels.daily_actions_below_ai(prefs, overrides.get(team_id)), autonomy.at_ai(pilot, autonomy.DAILY_ACTIONS)


async def _team(team_id: int, user_id: int) -> tuple[Any, Any]:
    """The team as the routes get it (owned, credentials hydrated), or (None, None)."""
    from api.deps import ensure_team_owned, load_owned_league_info
    team = await run_db("daily_actions_job.team", ensure_team_owned, team_id, user_id)
    if team is None:
        return None, None
    return team, await load_owned_league_info(team)


# ------------------------------- service ------------------------------- #


class DailyActionsJob:

    @staticmethod
    async def run(req: DailyActionsRunReq) -> DailyActionsRunResp:
        ir_moves: list[LineupMoveResult] = []
        adds: list[DailyActionsAdd] = []
        data: Optional[DailyActionsData] = None
        team_name = ""

        def done(outcome: str, *, reason: Optional[str] = None, held: Optional[str] = None) -> DailyActionsRunResp:
            if (ir_moves or adds) and outcome != "applied":
                # A later step didn't go: what did go stands, and `held` says what didn't
                outcome, held, reason = "applied", held or reason, None
            state = data.lineup if data is not None else None
            out = DailyActionsRunData(
                outcome=outcome, reason=reason, ir_moves=ir_moves, adds=adds, held=held,
                open_spots=open_spots(state) if state is not None else 0,
                nba_date=state.nba_date if state is not None else None,
                first_game_time_et=state.first_game_time_et if state is not None else None,
                team_name=(state.team_name if state is not None else "") or team_name,
            )
            if outcome != "skipped" or reason not in ("not_act", "at_ai"):
                log.info("daily_actions_run", team_id=req.team_id, outcome=outcome, reason=reason, held=held,
                         ir_moves=len(ir_moves), adds=len(adds))
            return DailyActionsRunResp(status=ApiStatus.SUCCESS, message=f"Daily actions {outcome}", data=out)

        level, at_ai = await _level_of(req.team_id, req.user_id)
        if at_ai:
            return done("skipped", reason="at_ai")
        if level != autonomy.ACT:
            return done("skipped", reason="not_act")
        if not settings.roster_writes_enabled:
            return done("skipped", reason="writes_disabled")
        team, league_info = await _team(req.team_id, req.user_id)
        if team is None:
            return done("skipped", reason="not_owner")
        team_name = league_info.team_name or ""
        caps = get_provider_adapter(league_info.provider).capabilities(league_info)
        if not (caps.lineup_read and caps.transactions):
            return done("skipped", reason="provider_not_supported")

        async def read() -> DailyActionsData:
            resp = await DailyActionsService.read(team, league_info)
            return resp.data if resp.data is not None else DailyActionsData()

        try:
            data = await read()
        except ProviderAuthError as exc:
            return done("rejected", reason=f"PROVIDER_AUTH_EXPIRED: {exc.message}")
        except Exception as exc:  # the board couldn't be read: the next poll asks again
            log.warning("daily_actions_read_failed", team_id=req.team_id, error=f"{type(exc).__name__}: {exc}")
            return done("failed", reason="read_failed")

        state = data.lineup
        if state is None or not data.can_write:
            return done("skipped", reason=f"can_write:{data.write_blocked_reason}")
        if state.nba_date != req.nba_date.isoformat():
            return done("skipped", reason="date_mismatch")
        if tipped(state, _now()):
            return done("skipped", reason="after_first_tip")

        pending = await run_db("daily_actions_job.pending", _pending, req.team_id)
        slots = _roster_slots(team)
        held: Optional[str] = None
        unread = False
        sent: set[str] = set()
        for writes in range(MAX_WRITES + 1):
            step = next_step(data, pending, adds_left=writes < MAX_WRITES)
            if step.kind == "done":
                break
            if step.kind in ("held", "unread"):
                held = step.why
                unread = step.kind == "unread"
                if unread:
                    log.warning("daily_actions_pool_unread", team_id=req.team_id, error=data.streamers_error)
                break
            if writes >= MAX_WRITES:
                break
            if any(a.id in sent for a in step.rows):
                # ESPN's read still trails a move already sent: never send it twice
                held = "ESPN didn't show the last move yet, so nothing more was sent"
                break
            state = data.lineup
            if tipped(state, _now()):
                # The pass ran into the tip (a write and its reads take a few seconds): what went stands
                held = TIPPED_HELD
                break
            sent.update(a.id for a in step.rows)
            try:
                if step.kind == "ir":
                    after, _, _ = await LineupEditorService._write(
                        user_id=req.user_id, team_id=req.team_id, league_info=league_info, state=state,
                        moves=_moves_of(step.rows), source=SOURCE, fallback_slot_counts=slots,
                    )
                    ir_moves.extend(_results_of(step.rows))
                else:
                    row = step.rows[0]
                    add_id = row.transaction.pickup.player_id
                    _, entry = await RosterTransactionService._validate(league_info, state, add_id, None)
                    if tipped(state, _now()):
                        held = TIPPED_HELD
                        break
                    names = {add_id: entry.name if entry is not None else row.player.name}
                    after, verified, audit_id = await RosterTransactionService._write(
                        user_id=req.user_id, team_id=req.team_id, league_info=league_info, state=state,
                        add=add_id, drop=None, moves=_moves_json(add_id, None, names), fallback_slot_counts=slots,
                        source=SOURCE,
                    )
                    adds.append(DailyActionsAdd(player=row.player, detail=row.detail, verified=verified,
                                                audit_id=audit_id))
                # The board the write confirmed (the pre-write one when its check-read failed): the
                # result reports it if the read below fails, and a good read replaces it
                data = data.model_copy(update={"lineup": after})
            except RosterTransactionInvalid as exc:
                return done("skipped", reason=(exc.data or {}).get("reason") or "add_invalid", held=exc.message)
            except RosterWriteRejected as exc:
                return done("rejected", reason=exc.message, held=f"ESPN refused it: {exc.message}")
            except ProviderAuthError as exc:
                return done("rejected", reason=f"PROVIDER_AUTH_EXPIRED: {exc.message}",
                            held="ESPN no longer accepts this league's credentials")
            except RosterWriteUnavailable as exc:
                return done("failed", reason=exc.message, held="ESPN couldn't be reached")
            except Exception as exc:  # e.g. the audit update after ESPN took it: the move may stand
                log.error("daily_actions_write_uncertain", team_id=req.team_id, step=step.kind, error=str(exc))
                return done("failed", reason="write_uncertain", held="A move was sent but not confirmed")
            try:
                data = await read()
            except Exception as exc:  # the write went; what follows waits for tomorrow's pass
                log.warning("daily_actions_reread_failed", team_id=req.team_id, error=str(exc))
                held = "The roster couldn't be read again after the last move"
                break

        if not ir_moves and not adds:
            if unread:
                # Nothing went and a spot is open: the next poll reads the pool again (a noop would settle the day)
                return done("failed", reason="pool_unread", held=held)
            return done("noop", held=held)
        return done("applied", held=held)
