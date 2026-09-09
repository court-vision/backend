"""
Add/drop transactions: read the board, check the request against it and ESPN's
player pool, send it through the private fantasy-writer, verify by re-reading,
and audit in usr.roster_moves as a `kind='transaction'` row.

One entry point:

    apply    POST /teams/{id}/roster/transactions    pick up and/or release one player

It reuses the lineup editor's gates and errors (the writes switch, the
provider, the board's `can_write`, the stale check) and mirrors its write
chain, so a client that handles lineup moves already handles everything here
— plus ROSTER_TRANSACTION_INVALID (422, `data.reason`) for a request the board
or the pool refuses before ESPN is asked. Roster capacity and acquisition
limits are deliberately NOT pre-checked: ESPN is the authority, and its
refusal arrives as ROSTER_WRITE_REJECTED carrying ESPN's own sentence.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from core.errors import AppError, ProviderAuthError
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.roster_transaction import (
    RosterTransactionData,
    RosterTransactionPlayer,
    RosterTransactionReq,
    RosterTransactionResp,
)
from services import fantasy_writer_client
from services.espn_service import EspnService, PoolEntry
from services.fantasy_writer_client import (
    FantasyWriterAuthRejected,
    FantasyWriterRejected,
    FantasyWriterUnavailable,
)
from services.lineup_editor_service import (
    RosterStale,
    RosterWriteBlocked,
    RosterWriteDisabled,
    RosterWriteRejected,
    RosterWriteUnavailable,
    _audit_insert,
    _audit_update,
    _roster_slots,
    writer_payload,
)
from services.lineup_read_service import LineupReadService

log = get_logger("roster_transaction")

NOT_ESPN_MESSAGE = "Roster changes are available for ESPN teams"


class RosterTransactionInvalid(AppError):
    """The board or ESPN's pool refuses the request before it is sent. `data.reason` is
    one of: nothing_to_do, same_player, drop_not_on_roster, drop_locked,
    add_already_on_roster, add_not_found, add_on_waivers, add_not_available, add_locked."""

    status_code = 422
    api_status = ApiStatus.VALIDATION_ERROR
    error_code = "ROSTER_TRANSACTION_INVALID"
    default_message = "That roster change is not possible"
    log_level = "info"


def _invalid(reason: str, message: str, player_id: Optional[int] = None) -> RosterTransactionInvalid:
    return RosterTransactionInvalid(message=message, data={"reason": reason, "player_id": player_id})


# ------------------------------- shapes ------------------------------- #


def idempotency_key(team_id: int, state: LineupState, add: Optional[int], drop: Optional[int]) -> str:
    return f"{team_id}:txn:{state.scoring_period_id}:{state.roster_version}:{add or 0}:{drop or 0}"


def transaction_payload(league_info: LeagueInfo, state: LineupState, add: Optional[int], drop: Optional[int],
                        key: str) -> dict[str, Any]:
    """The lineup envelope with `add_player_id` / `drop_player_id` in place of `moves`."""
    payload = writer_payload(league_info, state, [], key)
    del payload["moves"]
    payload["add_player_id"] = add
    payload["drop_player_id"] = drop
    return payload


def _moves_json(add: Optional[int], drop: Optional[int], names: dict[int, str]) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    if add is not None:
        moves.append({"player_id": add, "action": "add", "name": names.get(add, str(add))})
    if drop is not None:
        moves.append({"player_id": drop, "action": "drop", "name": names.get(drop, str(drop))})
    return moves


def _player(player_id: int, name: str, team: str) -> RosterTransactionPlayer:
    return RosterTransactionPlayer(player_id=player_id, name=name, team=team)


# ------------------------------- service ------------------------------- #


class RosterTransactionService:

    @staticmethod
    async def apply(team, league_info: LeagueInfo, req: RosterTransactionReq) -> RosterTransactionResp:
        if not settings.roster_writes_enabled:
            raise RosterWriteDisabled()
        if league_info.provider != FantasyProvider.ESPN:
            raise RosterWriteBlocked(message=NOT_ESPN_MESSAGE, data={"reason": "provider_not_supported"})

        slots = _roster_slots(team)
        state = await LineupReadService.read(team.team_id, league_info, fallback_slot_counts=slots)
        if not state.can_write:
            raise RosterWriteBlocked(data={"reason": state.write_blocked_reason})
        if state.roster_version != req.roster_version or state.scoring_period_id != req.expected_scoring_period_id:
            raise RosterStale(data={"lineup": state.model_dump(mode="json")})

        add, drop = req.add_player_id, req.drop_player_id
        holder, pool_entry = await RosterTransactionService._validate(league_info, state, add, drop)

        names = {p.player_id: p.name for p in state.players}
        if pool_entry is not None:
            names[pool_entry.player_id] = pool_entry.name
        fresh, verified, audit_id = await RosterTransactionService._write(
            user_id=team.user_id, team_id=team.team_id, league_info=league_info, state=state,
            add=add, drop=drop, moves=_moves_json(add, drop, names), fallback_slot_counts=slots,
        )

        added = _player(add, pool_entry.name, pool_entry.pro_team) if add is not None and pool_entry is not None else None
        dropped = _player(drop, holder.name, holder.team) if drop is not None and holder is not None else None
        parts = [f"Added {added.name}"] if added else []
        parts += [f"Dropped {dropped.name}"] if dropped else []
        return RosterTransactionResp(
            status=ApiStatus.SUCCESS,
            message=", ".join(parts) + " — sent to ESPN" + ("" if verified else ", not yet confirmed"),
            data=RosterTransactionData(lineup=fresh, added=added, dropped=dropped, verified=verified,
                                       audit_id=audit_id, scoring_period_id=fresh.scoring_period_id),
        )

    # ---- validation ----

    @staticmethod
    async def _validate(league_info: LeagueInfo, state: LineupState, add: Optional[int], drop: Optional[int],
                        ) -> tuple[Optional[LineupPlayer], Optional[PoolEntry]]:
        """The board's answer for the drop and ESPN's pool answer for the add — or the
        first reason the request cannot go. Only the add needs the pool lookup."""
        if add is None and drop is None:
            raise _invalid("nothing_to_do", "Choose a player to add or a player to drop")
        if add is not None and add == drop:
            raise _invalid("same_player", "The player to add and the player to drop are the same", add)

        on_board = {p.player_id: p for p in state.players}
        holder: Optional[LineupPlayer] = None
        if drop is not None:
            holder = on_board.get(drop)
            if holder is None:
                raise _invalid("drop_not_on_roster", "That player is not on your roster", drop)
            if holder.locked:
                raise _invalid("drop_locked", f"{holder.name} is locked for today and cannot be dropped", drop)

        pool_entry: Optional[PoolEntry] = None
        if add is not None:
            if add in on_board:
                raise _invalid("add_already_on_roster", f"{on_board[add].name} is already on your roster", add)
            entries = await EspnService.get_player_pool_entries(league_info, [add], scoring_period_id=state.scoring_period_id)
            pool_entry = entries.get(add)
            if pool_entry is None:
                raise _invalid("add_not_found", "ESPN does not list that player in this league", add)
            if pool_entry.status == "WAIVERS":
                until = f" until {pool_entry.waivers_until.isoformat()}" if pool_entry.waivers_until else ""
                raise _invalid("add_on_waivers", f"{pool_entry.name} is on waivers{until} — place the claim on ESPN", add)
            if pool_entry.status == "ONTEAM" or pool_entry.on_team_id:
                raise _invalid("add_not_available", f"{pool_entry.name} is on another team's roster", add)
            if pool_entry.roster_locked:
                raise _invalid("add_locked", f"{pool_entry.name} is locked for today and cannot be added", add)
        return holder, pool_entry

    # ---- the write chain (mirrors LineupEditorService._write) ----

    @staticmethod
    async def _write(*, user_id: int, team_id: int, league_info: LeagueInfo, state: LineupState,
                     add: Optional[int], drop: Optional[int], moves: list[dict[str, Any]],
                     fallback_slot_counts) -> tuple[LineupState, bool, int]:
        if not state.nba_date or not state.scoring_period_id:
            raise RosterWriteBlocked(data={"reason": "no_scoring_period"})
        key = idempotency_key(team_id, state, add, drop)
        audit_id = await run_db("roster_txn.audit_insert", _audit_insert, user_id, team_id,
                                date.fromisoformat(state.nba_date), state.scoring_period_id, "manual",
                                moves, key, kind="transaction")
        try:
            result = await fantasy_writer_client.apply_transaction(transaction_payload(league_info, state, add, drop, key))
        except FantasyWriterRejected as exc:
            await run_db("roster_txn.audit_update", _audit_update, audit_id, "rejected",
                         provider_status=exc.espn_status, error=exc.message)
            raise RosterWriteRejected(message=exc.message, data={"espn_status": exc.espn_status,
                                                                  "espn_error_code": exc.espn_error_code}) from exc
        except FantasyWriterAuthRejected as exc:
            await run_db("roster_txn.audit_update", _audit_update, audit_id, "failed",
                         provider_status=exc.espn_status, error="provider_auth_rejected")
            raise ProviderAuthError("espn") from exc
        except FantasyWriterUnavailable as exc:
            await run_db("roster_txn.audit_update", _audit_update, audit_id, "failed",
                         provider_status=exc.espn_status, error=exc.message)
            raise RosterWriteUnavailable() from exc

        try:
            fresh = await LineupReadService.read(team_id, league_info, fallback_slot_counts=fallback_slot_counts)
        except Exception as exc:
            # ESPN took the transaction and only the read-back failed: settle the row as applied,
            # just unverified, and hand back the pre-write board (same as the lineup chain).
            await run_db("roster_txn.audit_update", _audit_update, audit_id, "applied_unverified",
                         provider_status=result.espn_status, error=f"verify_read_failed: {exc}")
            log.warning("roster_transaction_unverified", team_id=team_id, add=add, drop=drop,
                        espn_status=result.espn_status, error=str(exc))
            return state, False, audit_id

        ids = {p.player_id for p in fresh.players}
        verified = (add is None or add in ids) and (drop is None or drop not in ids)
        await run_db("roster_txn.audit_update", _audit_update, audit_id,
                     "applied" if verified else "applied_unverified", provider_status=result.espn_status)
        log.info("roster_transaction_applied", team_id=team_id, add=add, drop=drop, verified=verified,
                 espn_status=result.espn_status, idempotent_replay=result.idempotent_replay)
        return fresh, verified, audit_id
