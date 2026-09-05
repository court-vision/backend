"""
Reconcile a draft session with the ESPN draft room's INIT snapshot.

The Draft Tap extension relays live picks to the frontend, which records them
through the ordinary pick endpoint. INIT is the other half: the room's full
state on every connect, decoded here (never in the browser) and folded into the
session in one transaction. It is the backfill for a late join and the
reconciliation for a reconnect — so it is idempotent: picks the session already
holds are skipped, and picks that disagree with what is recorded are reported,
never overwritten.

The heavy lifting is reused from `draft_service`: player resolution, the
strongest-shared-identity duplicate rule, pick geometry, the slot/length
validators, and the response shaper. This service only decodes, classifies, and
inserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, Union

from peewee import IntegrityError

from core.errors import BadRequestError, ConflictError
from db.base import db, db_operation
from db.models.drafts import DraftPick, DraftSession
from schemas.common import ApiStatus
from schemas.draft import (
    DraftInitSyncRequest,
    DraftInitSyncResp,
    DraftInitSyncResponse,
    DraftPickCreate,
    DraftSyncConflict,
)
from services.draft_service import (
    espn_league_id_of,
    DraftService,
    check_length_holds_picks,
    check_slot_in_range,
    duplicate_pick_of,
    matching_keeper,
    pick_for_slot,
    pick_geometry,
    pick_placement,
    round_of,
    total_picks_of,
    _keepers_of,
    _session_resp,
)
from utils.espn_draft_init import (
    decode_init,
    draft_type_of,
    espn_front,
    made_picks,
    my_slot_of,
    pick_order_of,
    position_limits_of,
    rounds_of,
    strip_init_prefix,
)

# ESPN's draft-state code in INIT and STATE frames: 0 before, 1 during, 2 after, 3 paused.
ESPN_AFTER_DRAFT = 2


@dataclass(frozen=True)
class InitHeader:
    espn_league_id: int
    espn_team_id: int
    draft_state: int
    draft_type: str
    pick_order: list[int]
    my_slot: Optional[int]
    rounds: Optional[int]
    espn_front: int


def derive_header(decoded: dict) -> InitHeader:
    """The session-shaping facts an INIT carries: order, slot, length, type."""
    order = pick_order_of(decoded)
    league = decoded.get("league") or {}
    return InitHeader(
        espn_league_id=int(decoded["leagueId"]),
        espn_team_id=int(decoded["teamId"]),
        draft_state=int(league.get("draftState", 0)),
        draft_type=draft_type_of(decoded),
        pick_order=order,
        my_slot=my_slot_of(decoded, order),
        rounds=rounds_of(decoded),
        espn_front=espn_front(decoded),
    )


def header_warnings(header: InitHeader, session) -> list[str]:
    """How a session that already has picks disagrees with the room's header.

    Reported, not applied: rewriting the header on a live session would re-derive
    every recorded pick's round and slot. The user fixes it with the ordinary
    PATCH.
    """
    warnings: list[str] = []
    order = [int(t) for t in (session.pick_order or [])]
    if header.pick_order and order and header.pick_order != order:
        warnings.append("pick order differs from the ESPN room — fix it in the room header")
    if header.my_slot is not None and session.my_slot is not None and header.my_slot != session.my_slot:
        warnings.append(f"slot: session says {session.my_slot}, ESPN says {header.my_slot}")
    if header.rounds and session.rounds and header.rounds != session.rounds:
        warnings.append(f"rounds: session has {session.rounds}, ESPN has {header.rounds}")
    if header.draft_type != session.draft_type:
        warnings.append(f"draft type: session is {session.draft_type}, ESPN is {header.draft_type}")
    return warnings


def classify_pick(
    existing: list,
    pick_number: int,
    espn_player_id: int,
    player_id: Optional[int],
    player_name: Optional[str],
) -> Union[str, DraftSyncConflict]:
    """`"insert"`, `"skip"`, or a conflict for one made INIT pick.

    - same number AND same player  -> skip (already recorded; a normal reconnect)
    - same number, a different player -> pick_number_taken
    - the player already sits elsewhere -> player_already_drafted (with held_at)
    """
    at_number = next((p for p in existing if p.overall_pick == pick_number), None)
    if at_number is not None:
        same = duplicate_pick_of([at_number], player_id, espn_player_id, player_name) is not None
        if same:
            return "skip"
        return DraftSyncConflict(
            pick_number=pick_number,
            espn_player_id=espn_player_id,
            reason="pick_number_taken",
            held_espn_player_id=at_number.espn_player_id,
            message=f"Pick {pick_number} is already a different player in this session",
        )
    held_at = duplicate_pick_of(existing, player_id, espn_player_id, player_name)
    if held_at is not None:
        return DraftSyncConflict(
            pick_number=pick_number,
            espn_player_id=espn_player_id,
            reason="player_already_drafted",
            held_at=held_at,
            message=f"That player is already recorded at pick {held_at}",
        )
    return "insert"


class DraftSyncService:
    @staticmethod
    @db_operation("drafts.sync_init")
    def sync_init(session_id: int, req: DraftInitSyncRequest) -> DraftInitSyncResponse:
        # 1. Decode. A payload that does not consume exactly is a truncated or
        #    wrong-version frame, not our snapshot.
        try:
            decoded = decode_init(strip_init_prefix(req.payload))
        except Exception as exc:  # ValueError / EOFError / binascii.Error
            raise BadRequestError("DRAFT_INIT_INVALID", "That INIT frame could not be decoded") from exc
        if decoded.get("_bytes_consumed") != decoded.get("_bytes_total"):
            raise BadRequestError("DRAFT_INIT_INVALID", "That INIT frame did not decode cleanly")

        # 2. Load the session (with its league).
        session = DraftService._session_or_404(session_id)
        header = derive_header(decoded)

        # 3. Which ESPN draft this room follows. A linked room accepts only its
        #    own; a live room from before linking existed falls back to its
        #    league's provider id. A mock room links to the first room it
        #    reconciles with (the client asks the user before posting) and is
        #    exclusive from then on: one active room per user per ESPN draft.
        expected = session.espn_league_id
        if expected is None and session.kind == "live" and session.league_id is not None:
            expected = espn_league_id_of(session.league)
        if expected is not None and header.espn_league_id != expected:
            raise ConflictError(
                "DRAFT_INIT_LEAGUE_MISMATCH",
                f"That ESPN room is league {header.espn_league_id}, this session is league {expected}",
                data={"espn_league_id": header.espn_league_id, "session_league_id": expected},
            )
        link: Optional[int] = None
        if session.espn_league_id is None and session.kind in ("live", "mock"):
            # A room CV has simulated into cannot then start following an ESPN
            # draft: the numbers are already spent, so every INIT pick would
            # come back a conflict. The refusal is the mirror of the
            # autopicker's own — a room follows a real draft or plays a
            # simulated one, never both.
            if DraftPick.select().where(
                (DraftPick.session == session_id) & (DraftPick.source == "mock")
            ).exists():
                raise ConflictError(
                    "DRAFT_ROOM_IS_SIMULATED",
                    "This room holds simulated picks; open a fresh room to follow an ESPN draft",
                )
            DraftService._check_room_free(session.user_id, header.espn_league_id, exclude_id=session.id)
            link = header.espn_league_id

        inserted = 0
        skipped = 0
        conflicts: list[DraftSyncConflict] = []
        warnings: list[str] = []
        header_applied = False

        with db.atomic():
            existing = DraftService._picks_of(session_id)

            # 4. Header: written only onto an empty session (nothing to re-derive).
            if not existing:
                touched = []
                if header.pick_order:
                    session.pick_order = header.pick_order
                    touched.append(DraftSession.pick_order)
                if header.my_slot is not None:
                    session.my_slot = header.my_slot
                    touched.append(DraftSession.my_slot)
                if header.rounds is not None and session.rounds is None:
                    session.rounds = header.rounds
                    touched.append(DraftSession.rounds)
                if header.draft_type != session.draft_type:
                    session.draft_type = header.draft_type
                    touched.append(DraftSession.draft_type)
                if touched:
                    league_size = len(session.pick_order or []) or None
                    check_slot_in_range(session.my_slot, league_size)
                    check_length_holds_picks(total_picks_of(session), [])
                    session.updated_at = datetime.utcnow()
                    session.save(only=touched + [DraftSession.updated_at])
                    header_applied = True
            else:
                warnings = header_warnings(header, session)

            league_size = len(session.pick_order or []) or None
            keeper_additions: list[dict] = []
            existing_keepers = _keepers_of(session)

            # 5. Fold in each made pick, ascending.
            for made in made_picks(decoded):
                pn = int(made["pickNumber"])
                espn_id = int(made["playerId"])
                player = DraftService._resolve_player(DraftPickCreate(espn_player_id=espn_id))
                player_id = player.id if player is not None else None
                player_name = player.name if player is not None else None

                verdict = classify_pick(existing, pn, espn_id, player_id, player_name)
                if verdict == "skip":
                    skipped += 1
                    continue
                if isinstance(verdict, DraftSyncConflict):
                    conflicts.append(verdict)
                    continue

                team_id = int(made["teamId"])
                by_me = team_id == header.espn_team_id
                source = "espn_sync"
                addition = None
                if made.get("isKeeper"):
                    source, addition = DraftSyncService._keeper_source(
                        pn, espn_id, by_me, session, league_size, existing_keepers, keeper_additions, warnings
                    )

                bid = None
                if session.draft_type == "auction":
                    amount = made.get("bidAmount") or 0
                    bid = amount if amount > 0 else None

                pick_round, pick_slot = pick_placement(
                    pn, league_size, session.draft_type, session.pick_order, team_id
                )
                try:
                    # A savepoint: a racing insert becomes a reported conflict
                    # rather than poisoning the whole reconciliation.
                    with db.atomic():
                        pick = DraftPick.create(
                            session_id=session_id,
                            overall_pick=pn,
                            round=pick_round,
                            slot=pick_slot,
                            player_id=player_id,
                            espn_player_id=espn_id,
                            espn_team_id=team_id,
                            player_name=player_name,
                            by_me=by_me,
                            source=source,
                            bid=bid,
                        )
                    existing.append(pick)
                    inserted += 1
                    # Only a keeper pick that actually landed earns its
                    # designation: a rolled-back insert must not leave the
                    # session naming a keeper it never recorded.
                    if addition is not None:
                        keeper_additions.append(addition)
                except IntegrityError as exc:
                    err = DraftService._pick_conflict(exc, pn, player_name)
                    reason = "player_already_drafted" if err.error_code == "DRAFT_PLAYER_ALREADY_DRAFTED" else "pick_number_taken"
                    conflicts.append(
                        DraftSyncConflict(pick_number=pn, espn_player_id=espn_id, reason=reason, message=err.message)
                    )

            # 6. Persist any new keeper designations, the link, and the stamps.
            model_fields = []
            if keeper_additions:
                session.keepers = (session.keepers or []) + keeper_additions
                model_fields.append(DraftSession.keepers)
            if link is not None:
                session.espn_league_id = link
                model_fields.append(DraftSession.espn_league_id)
            if inserted and session.started_at is None:
                session.started_at = datetime.utcnow()
                model_fields.append(DraftSession.started_at)
            # The room is over when its picks fill the draft, or when ESPN's
            # own state says so (2 = after the draft) — whichever comes first.
            if session.status == "active" and (
                DraftService._closes_draft(session, len(existing)) or header.draft_state == ESPN_AFTER_DRAFT
            ):
                model_fields += DraftService._complete(session)
            if model_fields:
                session.updated_at = datetime.utcnow()
                try:
                    session.save(only=model_fields + [DraftSession.updated_at])
                except IntegrityError as exc:
                    raise DraftService._link_conflict(exc) from exc

        picks = DraftService._picks_of(session_id)
        data = DraftInitSyncResp(
            session=_session_resp(
                session,
                used_picks=[p.overall_pick for p in picks],
                picks=picks,
                keeper_count=DraftService._keeper_count_of(session),
                keeper_picks=[p.overall_pick for p in picks if p.source == "keeper"],
            ),
            espn_league_id=header.espn_league_id,
            espn_team_id=header.espn_team_id,
            draft_state=header.draft_state,
            draft_type=session.draft_type,
            made=len(made_picks(decoded)),
            inserted=inserted,
            skipped=skipped,
            conflicts=conflicts,
            warnings=warnings,
            espn_front=header.espn_front,
            header_applied=header_applied,
            position_limits=position_limits_of(decoded),
        )
        return DraftInitSyncResponse(
            status=ApiStatus.SUCCESS,
            message=f"INIT reconciled: {inserted} inserted, {skipped} skipped",
            data=data,
        )

    @staticmethod
    def _keeper_source(
        pick_number: int,
        espn_id: int,
        by_me: bool,
        session,
        league_size: Optional[int],
        existing_keepers: list,
        pending_additions: list[dict],
        warnings: list[str],
    ) -> tuple[str, Optional[dict]]:
        """Decide how an INIT keeper is stored.

        Another seat's keeper is a spent pick the front must step over, recorded
        `source="keeper", by_me=False` — never repriced (see `plan_keeper_moves`).
        My own keeper needs a designation the session can price at its own pick,
        or it is downgraded to an ordinary synced pick with a warning.
        """
        if not by_me:
            return "keeper", None

        round_number = round_of(pick_number, league_size) if league_size else None
        priced = (
            pick_for_slot(round_number, session.my_slot, league_size, session.draft_type)
            if round_number
            else None
        )
        if priced != pick_number:
            warnings.append(
                f"your keeper at pick {pick_number} does not sit at your slot's pick — recorded as an ordinary pick"
            )
            return "espn_sync", None

        probe = _probe(espn_player_id=espn_id)
        already = matching_keeper(existing_keepers, probe) is not None or any(
            k.get("espn_player_id") == espn_id for k in pending_additions
        )
        addition = None if already else {"espn_player_id": espn_id, "round": round_number}
        return "keeper", addition


def _probe(**kwargs):
    """A pick-shaped stand-in for `matching_keeper`, which compares identities."""
    defaults = {"player_id": None, "espn_player_id": None, "player_name": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)
