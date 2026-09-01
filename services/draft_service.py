"""
Draft Lab sessions and picks: the per-user state a draft room is made of.

A session is the header (which league, which slot, how many rounds, keepers)
and its picks are the record. The board itself is derived from them and never
stored — `DraftBoardService` recomputes it on every read.

Everything the provider already told us is prefilled at create time from the
team's synced league: draft type, pick order, rounds, keeper allowance. The one
field the user must supply is `my_slot`: `draft_settings.pick_order` holds ESPN
team ids, and nothing maps them back to `usr.teams`, so the room asks.

Errors are raised, not returned (`core.responses`' rule): a missing session or
player is a `NotFoundError`, a pick number already taken a `ConflictError`, and
anything the session's own shape forbids a `BadRequestError`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Optional

from peewee import JOIN, IntegrityError

from core.errors import BadRequestError, ConflictError, NotFoundError
from db.base import db, db_operation
from db.models.drafts import DraftPick, DraftSession
from db.models.leagues import League
from db.models.nba.players import Player
from db.models.teams import Team
from schemas.common import ApiStatus
from schemas.draft import (
    DraftKeeper,
    DraftPickCreate,
    DraftPickDeleteResponse,
    DraftPickResp,
    DraftPickResponse,
    DraftSessionCreate,
    DraftSessionListResponse,
    DraftSessionResp,
    DraftSessionResponse,
    DraftSessionUpdate,
)

# Roster slots that are not drafted for: the injury slot is filled from the
# roster, never from the draft board.
NON_DRAFTABLE_SLOTS = frozenset({"IR", ""})

# How far past the next pick we look for the caller's next turn. A round of a
# 30-team league is 30 picks; two rounds is a generous ceiling for a snake.
_MY_TURN_SEARCH_LIMIT = 200


# ---- pure draft arithmetic -------------------------------------------------


def rounds_from_roster_slots(roster_slots: Optional[Mapping[str, int]]) -> Optional[int]:
    """Draftable roster spots — starters plus bench, IR excluded.

    ESPN publishes no round count; the number of roster spots a manager has to
    fill *is* the round count for a full draft.
    """
    if not roster_slots:
        return None
    total = 0
    for slot, count in roster_slots.items():
        if str(slot).upper() in NON_DRAFTABLE_SLOTS:
            continue
        try:
            total += max(0, int(count))
        except (TypeError, ValueError):
            continue
    return total or None


def slot_of(overall_pick: int, league_size: int, draft_type: str = "snake") -> Optional[int]:
    """The 1-based slot in `pick_order` that owns `overall_pick`.

    Snake drafts reverse on even rounds — pick 11 of a 10-team draft belongs to
    slot 10 again, not slot 1. Auction drafts have no pick order at all.
    """
    if draft_type != "snake" or league_size < 1 or overall_pick < 1:
        return None
    index = (overall_pick - 1) % league_size
    round_number = (overall_pick - 1) // league_size + 1
    return index + 1 if round_number % 2 == 1 else league_size - index


def round_of(overall_pick: int, league_size: int, draft_type: str = "snake") -> Optional[int]:
    if draft_type != "snake" or league_size < 1 or overall_pick < 1:
        return None
    return (overall_pick - 1) // league_size + 1


def next_unused_pick(used: Iterable[int]) -> int:
    """The lowest unused pick number, so an undo mid-draft is re-fillable.

    Taking `max + 1` instead would leave a hole where a corrected pick was
    removed, and the hole would silently shift every later pick's slot.
    """
    taken = {int(p) for p in used}
    overall = 1
    while overall in taken:
        overall += 1
    return overall


def next_pick_for_slot(
    from_pick: int, slot: Optional[int], league_size: Optional[int], draft_type: str = "snake"
) -> Optional[int]:
    """The first pick at or after `from_pick` that belongs to `slot`."""
    if draft_type != "snake" or not slot or not league_size:
        return None
    for overall in range(max(1, from_pick), max(1, from_pick) + _MY_TURN_SEARCH_LIMIT):
        if slot_of(overall, league_size, draft_type) == slot:
            return overall
    return None


def total_picks_of(session: DraftSession) -> Optional[int]:
    """How many picks the draft holds, when both its size and length are known."""
    league_size = len(session.pick_order or [])
    if not league_size or not session.rounds:
        return None
    return league_size * session.rounds


def resolve_overall_pick(
    used: Iterable[int], requested: Optional[int], total_picks: Optional[int]
) -> int:
    """Which pick number a new pick takes, or why it cannot have one.

    Pure so the rule is testable without a database: the write path still
    relies on the unique (session_id, overall_pick) index to settle a race the
    `used` snapshot cannot see.
    """
    taken = {int(p) for p in used}
    overall = requested if requested is not None else next_unused_pick(taken)
    if overall in taken:
        raise ConflictError("DRAFT_PICK_ALREADY_EXISTS", f"Pick {overall} is already recorded")
    if total_picks is not None and overall > total_picks:
        raise BadRequestError(
            "DRAFT_PICK_OUT_OF_RANGE",
            f"Pick {overall} is past the end of a {total_picks}-pick draft",
        )
    return overall


def league_prefill(league: Optional[League]) -> dict:
    """Draft-room defaults from a synced league: type, order, rounds, keepers."""
    if league is None:
        return {"draft_type": "snake", "pick_order": [], "rounds": None, "keeper_count": None}

    settings = dict(getattr(league, "draft_settings", None) or {})
    raw_type = str(settings.get("type") or "").upper()
    order: list[int] = []
    for team_id in settings.get("pick_order") or []:
        try:
            order.append(int(team_id))
        except (TypeError, ValueError):
            continue
    keeper_count = settings.get("keeper_count")
    try:
        keeper_count = int(keeper_count) if keeper_count is not None else None
    except (TypeError, ValueError):
        keeper_count = None

    return {
        # ESPN's other types (OFFLINE, AUTOPICK) still draft in pick order.
        "draft_type": "auction" if raw_type == "AUCTION" else "snake",
        "pick_order": order,
        "rounds": rounds_from_roster_slots(getattr(league, "roster_slots", None)),
        "keeper_count": keeper_count,
    }


# ---- response shaping ------------------------------------------------------


def _keepers_of(session: DraftSession) -> list[DraftKeeper]:
    return [DraftKeeper(**k) for k in (session.keepers or []) if isinstance(k, dict)]


def _pick_resp(pick: DraftPick) -> DraftPickResp:
    return DraftPickResp(
        overall_pick=pick.overall_pick,
        round=pick.round,
        slot=pick.slot,
        player_id=pick.player_id,
        espn_player_id=pick.espn_player_id,
        player_name=pick.player_name,
        by_me=bool(pick.by_me),
        source=pick.source,
        bid=float(pick.bid) if pick.bid is not None else None,
        created_at=pick.created_at,
    )


def _session_resp(
    session: DraftSession,
    used_picks: Iterable[int],
    picks: Optional[list[DraftPick]] = None,
    keeper_count: Optional[int] = None,
) -> DraftSessionResp:
    used = sorted({int(p) for p in used_picks})
    order = [int(t) for t in (session.pick_order or []) if isinstance(t, (int, float))]
    league_size = len(order) or None
    next_overall = next_unused_pick(used)
    # Where the draft actually is, which is not where the next pick *number*
    # is: an undo mid-draft leaves a hole, and `next_overall` is that hole. My
    # turn is counted from the front, or a correction would say I am on the
    # clock two rounds ago.
    front = (used[-1] + 1) if used else 1
    my_next = next_pick_for_slot(front, session.my_slot, league_size, session.draft_type)

    return DraftSessionResp(
        id=session.id,
        team_id=session.team_id,
        league_id=session.league_id,
        kind=session.kind,
        status=session.status,
        draft_type=session.draft_type,
        pick_order=order,
        my_slot=session.my_slot,
        rounds=session.rounds,
        keepers=_keepers_of(session),
        league_size=league_size,
        keeper_count=keeper_count,
        total_picks=(league_size * session.rounds) if (league_size and session.rounds) else None,
        pick_count=len(used),
        next_overall_pick=next_overall,
        my_next_pick=my_next,
        picks_until_my_turn=(my_next - front) if my_next is not None else None,
        picks=[_pick_resp(p) for p in (picks or [])],
        started_at=session.started_at,
        completed_at=session.completed_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


class DraftService:
    """CRUD over `usr.draft_sessions` and `usr.draft_picks`."""

    # ---- sessions ----------------------------------------------------------

    @staticmethod
    @db_operation("drafts.create")
    def create_session(user_id: int, req: DraftSessionCreate) -> DraftSessionResponse:
        league: Optional[League] = None
        team: Optional[Team] = None
        if req.team_id is not None:
            team = Team.get_or_none((Team.team_id == req.team_id) & (Team.user_id == user_id))
            if team is None:
                raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
            league = team.league if team.league_id is not None else None

        prefill = league_prefill(league)
        draft_type = req.draft_type or prefill["draft_type"]
        pick_order = list(req.pick_order) if req.pick_order is not None else prefill["pick_order"]
        rounds = req.rounds if req.rounds is not None else prefill["rounds"]

        if req.my_slot is not None and pick_order and req.my_slot > len(pick_order):
            raise BadRequestError(
                "DRAFT_SLOT_OUT_OF_RANGE",
                f"Slot {req.my_slot} is outside a {len(pick_order)}-team draft",
            )

        session = DraftSession.create(
            user_id=user_id,
            team_id=req.team_id,
            league_id=league.id if league is not None else None,
            kind=req.kind,
            draft_type=draft_type,
            pick_order=pick_order,
            my_slot=req.my_slot,
            rounds=rounds,
            keepers=[k.model_dump(exclude_none=True) for k in req.keepers],
        )

        prefilled = league is not None and (req.pick_order is None or req.rounds is None)
        return DraftSessionResponse(
            status=ApiStatus.SUCCESS,
            message=(
                "Draft session created from your league settings" if prefilled
                else "Draft session created"
            ),
            data=_session_resp(session, used_picks=(), keeper_count=prefill["keeper_count"]),
        )

    @staticmethod
    @db_operation("drafts.list")
    def list_sessions(user_id: int) -> DraftSessionListResponse:
        sessions = list(
            DraftSession.select(DraftSession, League)
            .join(League, JOIN.LEFT_OUTER)
            .where(DraftSession.user == user_id)
            .order_by(DraftSession.created_at.desc(), DraftSession.id.desc())
        )
        if not sessions:
            return DraftSessionListResponse(
                status=ApiStatus.SUCCESS, message="No draft sessions yet", data=[]
            )

        counts = DraftService._pick_numbers_by_session([s.id for s in sessions])
        data = [
            _session_resp(
                s,
                used_picks=counts.get(s.id, ()),
                keeper_count=league_prefill(s.league if s.league_id is not None else None)["keeper_count"],
            )
            for s in sessions
        ]
        return DraftSessionListResponse(
            status=ApiStatus.SUCCESS,
            message=f"{len(data)} draft session{'s' if len(data) != 1 else ''} fetched",
            data=data,
        )

    @staticmethod
    @db_operation("drafts.get")
    def get_session(session_id: int) -> DraftSessionResponse:
        session = DraftService._session_or_404(session_id)
        picks = DraftService._picks_of(session_id)
        return DraftSessionResponse(
            status=ApiStatus.SUCCESS,
            message="Draft session fetched",
            data=_session_resp(
                session,
                used_picks=[p.overall_pick for p in picks],
                picks=picks,
                keeper_count=DraftService._keeper_count_of(session),
            ),
        )

    @staticmethod
    @db_operation("drafts.update")
    def update_session(session_id: int, req: DraftSessionUpdate) -> DraftSessionResponse:
        session = DraftService._session_or_404(session_id)
        fields = req.model_fields_set
        # Only the columns actually touched are written, so a PATCH never
        # clobbers a field a concurrent request changed. `my_slot` and `rounds`
        # are nullable and an explicit null clears them; the rest are NOT NULL
        # columns, where a null means "leave it alone".
        touched = [DraftSession.updated_at]

        if "pick_order" in fields and req.pick_order is not None:
            session.pick_order = list(req.pick_order)
            touched.append(DraftSession.pick_order)
        if "my_slot" in fields:
            order = session.pick_order or []
            if req.my_slot is not None and order and req.my_slot > len(order):
                raise BadRequestError(
                    "DRAFT_SLOT_OUT_OF_RANGE",
                    f"Slot {req.my_slot} is outside a {len(order)}-team draft",
                )
            session.my_slot = req.my_slot
            touched.append(DraftSession.my_slot)
        if "draft_type" in fields and req.draft_type is not None:
            session.draft_type = req.draft_type
            touched.append(DraftSession.draft_type)
        if "rounds" in fields:
            session.rounds = req.rounds
            touched.append(DraftSession.rounds)
        if "keepers" in fields and req.keepers is not None:
            session.keepers = [k.model_dump(exclude_none=True) for k in req.keepers]
            touched.append(DraftSession.keepers)
        if "status" in fields and req.status is not None:
            session.status = req.status
            touched.append(DraftSession.status)
            # A draft is only finished once; re-completing keeps the first time.
            if req.status == "completed" and session.completed_at is None:
                session.completed_at = datetime.utcnow()
                touched.append(DraftSession.completed_at)
        session.updated_at = datetime.utcnow()
        session.save(only=touched)

        picks = DraftService._picks_of(session_id)
        return DraftSessionResponse(
            status=ApiStatus.SUCCESS,
            message="Draft session updated",
            data=_session_resp(
                session,
                used_picks=[p.overall_pick for p in picks],
                picks=picks,
                keeper_count=DraftService._keeper_count_of(session),
            ),
        )

    # ---- picks -------------------------------------------------------------

    @staticmethod
    @db_operation("drafts.add_pick")
    def add_pick(session_id: int, req: DraftPickCreate) -> DraftPickResponse:
        session = DraftService._session_or_404(session_id)
        # No pick order means no seats: a guessed size would invent a round and
        # slot for every pick and bound the draft at a length nobody set.
        league_size = len(session.pick_order or []) or None

        with db.atomic():
            used = {
                row.overall_pick
                for row in DraftPick.select(DraftPick.overall_pick).where(DraftPick.session == session_id)
            }
            overall = resolve_overall_pick(used, req.overall_pick, total_picks_of(session))
            player = DraftService._resolve_player(req)
            try:
                pick = DraftPick.create(
                    session_id=session_id,
                    overall_pick=overall,
                    round=round_of(overall, league_size, session.draft_type) if league_size else None,
                    slot=slot_of(overall, league_size, session.draft_type) if league_size else None,
                    player_id=player.id if player is not None else None,
                    espn_player_id=req.espn_player_id or (player.espn_id if player is not None else None),
                    player_name=req.player_name or (player.name if player is not None else None),
                    by_me=req.by_me,
                    source=req.source,
                    bid=req.bid,
                )
            except IntegrityError as exc:
                # Two clicks landing together: the read above is advisory, the
                # unique (session_id, overall_pick) index is what decides.
                raise ConflictError(
                    "DRAFT_PICK_ALREADY_EXISTS", f"Pick {overall} is already recorded"
                ) from exc
            # The first recorded pick is when the draft actually started.
            if session.started_at is None:
                session.started_at = datetime.utcnow()
            session.updated_at = datetime.utcnow()
            session.save(only=[DraftSession.started_at, DraftSession.updated_at])

        unresolved = player is None
        return DraftPickResponse(
            status=ApiStatus.SUCCESS,
            message=(
                f"Pick {overall} recorded (player not in nba.players yet)" if unresolved
                else f"Pick {overall} recorded"
            ),
            data=_pick_resp(pick),
        )

    @staticmethod
    @db_operation("drafts.remove_pick")
    def remove_pick(session_id: int, overall_pick: int) -> DraftPickDeleteResponse:
        pick = DraftPick.get_or_none(
            (DraftPick.session == session_id) & (DraftPick.overall_pick == overall_pick)
        )
        if pick is None:
            raise NotFoundError("DRAFT_PICK_NOT_FOUND", f"Pick {overall_pick} is not recorded")
        pick.delete_instance()
        return DraftPickDeleteResponse(
            status=ApiStatus.SUCCESS, message=f"Pick {overall_pick} undone", data=overall_pick
        )

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _session_or_404(session_id: int) -> DraftSession:
        """Ownership was settled by `get_owned_session`; this reloads the row."""
        session = (
            DraftSession.select(DraftSession, League)
            .join(League, JOIN.LEFT_OUTER)
            .where(DraftSession.id == session_id)
            .first()
        )
        if session is None:
            raise NotFoundError("DRAFT_SESSION_NOT_FOUND", "Draft session not found")
        return session

    @staticmethod
    def _keeper_count_of(session: DraftSession) -> Optional[int]:
        league = session.league if session.league_id is not None else None
        return league_prefill(league)["keeper_count"]

    @staticmethod
    def _picks_of(session_id: int) -> list[DraftPick]:
        return list(
            DraftPick.select()
            .where(DraftPick.session == session_id)
            .order_by(DraftPick.overall_pick)
        )

    @staticmethod
    def _pick_numbers_by_session(session_ids: list[int]) -> dict[int, list[int]]:
        if not session_ids:
            return {}
        by_session: dict[int, list[int]] = {}
        rows = (
            DraftPick.select(DraftPick.session, DraftPick.overall_pick)
            .where(DraftPick.session.in_(session_ids))
        )
        for row in rows:
            by_session.setdefault(row.session_id, []).append(row.overall_pick)
        return by_session

    @staticmethod
    def _resolve_player(req: DraftPickCreate) -> Optional[Player]:
        """NBA id -> ESPN id -> normalized name, in that order.

        Only an NBA id we cannot find is an error: an ESPN id or a name the
        board has not resolved yet is exactly the lag `usr.draft_picks` was
        shaped to carry, so the pick is recorded provider-side either way.
        """
        if req.player_id is not None:
            player = Player.get_or_none(Player.id == req.player_id)
            if player is None:
                raise NotFoundError("PLAYER_NOT_FOUND", f"No NBA player {req.player_id}")
            return player
        if req.espn_player_id is not None:
            player = Player.get_or_none(Player.espn_id == req.espn_player_id)
            if player is not None:
                return player
        if req.player_name:
            return Player.find_by_name(req.player_name)
        return None
