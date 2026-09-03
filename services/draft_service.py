"""
Draft Lab sessions and picks: the per-user state a draft room is made of.

A session is the header (which league, which slot, how many rounds, keepers)
and its picks are the record. The board itself is derived from them and never
stored — `DraftBoardService` recomputes it on every read.

Everything the provider already told us is prefilled at create time from the
team's synced league: draft type, pick order, rounds, keeper allowance. The one
field the user must supply is `my_slot`: `draft_settings.pick_order` holds ESPN
team ids, and nothing maps them back to `usr.teams`, so the room asks.

A keeper is a pick spent before the draft starts. Recorded with `source:
keeper` at the pick its round costs, it leaves the board like any pick but
never counts as the draft front (`draft_front`), so whose-turn arithmetic steps
over it the way a real keeper draft skips a kept slot.

Errors are raised, not returned (`core.responses`' rule): a missing session or
player is a `NotFoundError`, a pick number already taken — or a player already
drafted — a `ConflictError`, and anything the session's own shape forbids a
`BadRequestError`.
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

# The partial unique index migration 0013 puts on (session_id, player_id). Its
# name in a driver error is how a racing duplicate player is told from a taken
# pick number, which has its own index.
PLAYER_UNIQUE_INDEX = "draft_picks_session_player_uq"


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
    from_pick: int,
    slot: Optional[int],
    league_size: Optional[int],
    draft_type: str = "snake",
    skip: Iterable[int] = (),
) -> Optional[int]:
    """The first pick at or after `from_pick` that belongs to `slot`.

    `skip` holds picks already spent — a keeper's — which are not a turn on
    the clock however much they belong to the slot.
    """
    if draft_type != "snake" or not slot or not league_size:
        return None
    spent = {int(p) for p in skip}
    for overall in range(max(1, from_pick), max(1, from_pick) + _MY_TURN_SEARCH_LIMIT):
        if overall not in spent and slot_of(overall, league_size, draft_type) == slot:
            return overall
    return None


def pick_for_slot(
    round_number: Optional[int],
    slot: Optional[int],
    league_size: Optional[int],
    draft_type: str = "snake",
) -> Optional[int]:
    """The overall pick `slot` makes in `round_number` — what a keeper costs."""
    if (
        draft_type != "snake"
        or not round_number or round_number < 1
        or not slot or not league_size or slot > league_size
    ):
        return None
    base = (round_number - 1) * league_size
    return base + (slot if round_number % 2 == 1 else league_size - slot + 1)


def draft_front(used: Iterable[int], keeper_picks: Iterable[int] = ()) -> int:
    """Where the draft is: one past the last pick actually made on the clock.

    Keeper picks are spent before the draft starts, so they neither move the
    front forward nor can the front land on one — it steps over them the way
    a real keeper draft skips a kept slot.
    """
    spent = {int(p) for p in keeper_picks}
    played = [int(p) for p in used if int(p) not in spent]
    front = (max(played) + 1) if played else 1
    while front in spent:
        front += 1
    return front


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


def pick_geometry(
    overall_pick: int, league_size: Optional[int], draft_type: str
) -> tuple[Optional[int], Optional[int]]:
    """(round, slot) of a pick — both None without a pick order, or in an auction.

    Derived, never authored: a pick's round and slot follow from its number
    and the session header, so a header change re-derives them (see
    `DraftService.update_session`).
    """
    if not league_size:
        return None, None
    return (
        round_of(overall_pick, league_size, draft_type),
        slot_of(overall_pick, league_size, draft_type),
    )


def check_slot_in_range(my_slot: Optional[int], league_size: Optional[int]) -> None:
    """A confirmed slot must be a seat the pick order actually has.

    Checked against the *resulting* session on every write, not just when the
    slot itself is in the request: shrinking the pick order under a confirmed
    slot would otherwise leave a seat whose turn can never come up.
    """
    if my_slot is not None and league_size and my_slot > league_size:
        raise BadRequestError(
            "DRAFT_SLOT_OUT_OF_RANGE", f"Slot {my_slot} is outside a {league_size}-team draft"
        )


def check_length_holds_picks(total_picks: Optional[int], used: Iterable[int]) -> None:
    """A draft cannot be resized shorter than the picks already recorded in it."""
    last = max((int(p) for p in used), default=0)
    if total_picks is not None and last > total_picks:
        raise BadRequestError(
            "DRAFT_SHORTER_THAN_PICKS",
            f"Pick {last} is already recorded; a {total_picks}-pick draft cannot hold it",
        )


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


# ---- player identity -------------------------------------------------------


def _normalize_name(name: Optional[str]) -> str:
    """The `nba.players.name_normalized` rule, so names compare the way lookups do."""
    return str(name).lower().strip() if name else ""


def duplicate_pick_of(
    existing: Iterable,
    player_id: Optional[int],
    espn_player_id: Optional[int],
    player_name: Optional[str],
) -> Optional[int]:
    """The overall pick already holding this player, or None.

    Each existing pick is compared at the strongest identity the two share:
    NBA id when both have one, else ESPN id, else normalized name. That is
    what lets a pick recorded before the player reached `nba.players` (ESPN id
    or name only) still collide with the same player recorded after he did —
    while two resolved players who happen to share a name stay distinct.
    """
    name_key = _normalize_name(player_name)
    for pick in existing:
        if player_id is not None and pick.player_id is not None:
            same = pick.player_id == player_id
        elif espn_player_id is not None and pick.espn_player_id is not None:
            same = pick.espn_player_id == espn_player_id
        else:
            same = bool(name_key) and _normalize_name(pick.player_name) == name_key
        if same:
            return pick.overall_pick
    return None


def resolve_lagging_picks(picks: Iterable) -> int:
    """Fill in `player_id`, in memory, on picks whose player has since been synced.

    A pick may be recorded with only an ESPN id or a name — the lag
    `usr.draft_picks` was shaped to carry. Once `nba.players` has the player,
    every reader must count the pick as his: the board would otherwise offer
    him again (and leave him out of the caller's cap count) from the day he
    synced. Resolution is ESPN id first, then a normalized name that matches
    exactly one player. Nothing is written; returns how many picks resolved.
    """
    lagging = [
        p for p in picks
        if p.player_id is None and (p.espn_player_id is not None or _normalize_name(p.player_name))
    ]
    if not lagging:
        return 0
    espn_ids = {p.espn_player_id for p in lagging if p.espn_player_id is not None}
    names = {_normalize_name(p.player_name) for p in lagging} - {""}
    clauses = []
    if espn_ids:
        clauses.append(Player.espn_id.in_(list(espn_ids)))
    if names:
        clauses.append(Player.name_normalized.in_(list(names)))
    where = clauses[0]
    for clause in clauses[1:]:
        where = where | clause

    by_espn: dict[int, int] = {}
    by_name: dict[str, set[int]] = {}
    for rec in Player.select(Player.id, Player.espn_id, Player.name_normalized).where(where):
        if rec.espn_id is not None:
            by_espn[rec.espn_id] = rec.id
        by_name.setdefault(rec.name_normalized, set()).add(rec.id)

    resolved = 0
    for pick in lagging:
        player_id = by_espn.get(pick.espn_player_id) if pick.espn_player_id is not None else None
        if player_id is None:
            candidates = by_name.get(_normalize_name(pick.player_name), set())
            player_id = next(iter(candidates)) if len(candidates) == 1 else None
        if player_id is not None:
            pick.player_id = player_id
            resolved += 1
    return resolved


# ---- response shaping ------------------------------------------------------


def _keepers_of(session: DraftSession) -> list[DraftKeeper]:
    """Keepers as stored, each stamped with the pick it consumes — once the
    session has a slot and a pick order, and the keeper a round to cost."""
    order = [t for t in (session.pick_order or []) if isinstance(t, (int, float))]
    league_size = len(order) or None
    keepers: list[DraftKeeper] = []
    for raw in session.keepers or []:
        if not isinstance(raw, dict):
            continue
        fields = {k: v for k, v in raw.items() if k != "overall_pick"}
        keepers.append(DraftKeeper(
            **fields,
            overall_pick=pick_for_slot(
                fields.get("round"), session.my_slot, league_size, session.draft_type
            ),
        ))
    return keepers


def _stored_keepers(keepers: Iterable[DraftKeeper]) -> list[dict]:
    """What a keeper row keeps: identity and round. Its pick is derived on read."""
    return [k.model_dump(exclude_none=True, exclude={"overall_pick"}) for k in keepers]


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
    keeper_picks: Iterable[int] = (),
) -> DraftSessionResp:
    used = sorted({int(p) for p in used_picks})
    spent = frozenset(int(p) for p in keeper_picks)
    order = [int(t) for t in (session.pick_order or []) if isinstance(t, (int, float))]
    league_size = len(order) or None
    next_overall = next_unused_pick(used)
    # Where the draft actually is, which is not where the next pick *number*
    # is: an undo mid-draft leaves a hole, and `next_overall` is that hole. My
    # turn is counted from the front, or a correction would say I am on the
    # clock two rounds ago. Keeper picks are spent before the draft starts and
    # never move the front — see `draft_front`.
    front = draft_front(used, spent)
    my_next = next_pick_for_slot(front, session.my_slot, league_size, session.draft_type, skip=spent)
    until = (
        my_next - front - sum(1 for k in spent if front <= k < my_next)
        if my_next is not None else None
    )

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
        picks_until_my_turn=until,
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

        check_slot_in_range(req.my_slot, len(pick_order) or None)

        session = DraftSession.create(
            user_id=user_id,
            team_id=req.team_id,
            league_id=league.id if league is not None else None,
            kind=req.kind,
            draft_type=draft_type,
            pick_order=pick_order,
            my_slot=req.my_slot,
            rounds=rounds,
            keepers=_stored_keepers(req.keepers),
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

        by_session = DraftService._picks_by_session([s.id for s in sessions])
        data = [
            _session_resp(
                s,
                used_picks=[n for n, _ in by_session.get(s.id, ())],
                keeper_count=league_prefill(s.league if s.league_id is not None else None)["keeper_count"],
                keeper_picks=[n for n, source in by_session.get(s.id, ()) if source == "keeper"],
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
                keeper_picks=[p.overall_pick for p in picks if p.source == "keeper"],
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
        reshaped = False    # pick order or draft type changed: picks re-derive

        if "pick_order" in fields and req.pick_order is not None:
            session.pick_order = list(req.pick_order)
            touched.append(DraftSession.pick_order)
            reshaped = True
        if "my_slot" in fields:
            session.my_slot = req.my_slot
            touched.append(DraftSession.my_slot)
        if "draft_type" in fields and req.draft_type is not None:
            session.draft_type = req.draft_type
            touched.append(DraftSession.draft_type)
            reshaped = True
        if "rounds" in fields:
            session.rounds = req.rounds
            touched.append(DraftSession.rounds)
        if "keepers" in fields and req.keepers is not None:
            session.keepers = _stored_keepers(req.keepers)
            touched.append(DraftSession.keepers)
        if "status" in fields and req.status is not None:
            session.status = req.status
            touched.append(DraftSession.status)
            # A draft is only finished once; re-completing keeps the first time.
            if req.status == "completed" and session.completed_at is None:
                session.completed_at = datetime.utcnow()
                touched.append(DraftSession.completed_at)

        with db.atomic():
            picks = DraftService._picks_of(session_id)
            # It is the resulting session that has to hold together, whichever
            # fields the request carried: a pick order shrunk under a confirmed
            # slot, or a draft resized shorter than the picks already in it,
            # would leave a room whose own numbers disagree.
            league_size = len(session.pick_order or []) or None
            check_slot_in_range(session.my_slot, league_size)
            check_length_holds_picks(total_picks_of(session), (p.overall_pick for p in picks))
            # A pick's round and slot are derived from its number, the pick
            # order and the draft type. When either of the latter changes, every
            # recorded pick is re-derived in the same transaction — otherwise a
            # snake turned auction would still show rounds, and a resized order
            # would credit picks to seats that no longer exist.
            if reshaped:
                # One UPDATE per pick rather than bulk_update: its single CASE
                # statement has no column to type NULLs against, and turning a
                # snake into an auction makes every value NULL. This runs once
                # per header change, over at most a draft's worth of rows.
                for pick in picks:
                    pick.round, pick.slot = pick_geometry(
                        pick.overall_pick, league_size, session.draft_type
                    )
                    pick.save(only=[DraftPick.round, DraftPick.slot])
            session.updated_at = datetime.utcnow()
            session.save(only=touched)

        return DraftSessionResponse(
            status=ApiStatus.SUCCESS,
            message="Draft session updated",
            data=_session_resp(
                session,
                used_picks=[p.overall_pick for p in picks],
                picks=picks,
                keeper_count=DraftService._keeper_count_of(session),
                keeper_picks=[p.overall_pick for p in picks if p.source == "keeper"],
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
            existing = list(
                DraftPick.select(
                    DraftPick.overall_pick, DraftPick.player, DraftPick.espn_player_id, DraftPick.player_name
                ).where(DraftPick.session == session_id)
            )
            overall = resolve_overall_pick(
                (p.overall_pick for p in existing), req.overall_pick, total_picks_of(session)
            )
            player = DraftService._resolve_player(req)
            player_id = player.id if player is not None else None
            espn_player_id = req.espn_player_id or (player.espn_id if player is not None else None)
            player_name = req.player_name or (player.name if player is not None else None)
            # One player, one pick. Recorded twice he would inflate pick_count
            # while the board, which collapses picks to a set of ids, could only
            # hide him once; a correction goes through undo, not a second entry.
            held_at = duplicate_pick_of(existing, player_id, espn_player_id, player_name)
            if held_at is not None:
                raise ConflictError(
                    "DRAFT_PLAYER_ALREADY_DRAFTED",
                    f"{player_name or 'That player'} is already recorded at pick {held_at}",
                )
            pick_round, pick_slot = pick_geometry(overall, league_size, session.draft_type)
            try:
                pick = DraftPick.create(
                    session_id=session_id,
                    overall_pick=overall,
                    round=pick_round,
                    slot=pick_slot,
                    player_id=player_id,
                    espn_player_id=espn_player_id,
                    player_name=player_name,
                    by_me=req.by_me,
                    source=req.source,
                    bid=req.bid,
                )
            except IntegrityError as exc:
                # Two clicks landing together: the reads above are advisory,
                # the unique indexes are what decide — and which one fired
                # says whether the number or the player was taken.
                raise DraftService._pick_conflict(exc, overall, player_name) from exc
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
        picks = list(
            DraftPick.select()
            .where(DraftPick.session == session_id)
            .order_by(DraftPick.overall_pick)
        )
        # A pick recorded before its player reached nba.players reports him
        # once he has, without waiting for anything to rewrite the row.
        resolve_lagging_picks(picks)
        return picks

    @staticmethod
    def _pick_conflict(exc: IntegrityError, overall: int, player_name: Optional[str]) -> ConflictError:
        """The 409 for whichever unique index a racing insert tripped."""
        if PLAYER_UNIQUE_INDEX in str(exc):
            return ConflictError(
                "DRAFT_PLAYER_ALREADY_DRAFTED",
                f"{player_name or 'That player'} is already recorded in this draft",
            )
        return ConflictError("DRAFT_PICK_ALREADY_EXISTS", f"Pick {overall} is already recorded")

    @staticmethod
    def _picks_by_session(session_ids: list[int]) -> dict[int, list[tuple[int, str]]]:
        """Session id -> [(overall_pick, source)]: enough to place the front."""
        if not session_ids:
            return {}
        by_session: dict[int, list[tuple[int, str]]] = {}
        rows = (
            DraftPick.select(DraftPick.session, DraftPick.overall_pick, DraftPick.source)
            .where(DraftPick.session.in_(session_ids))
        )
        for row in rows:
            by_session.setdefault(row.session_id, []).append((row.overall_pick, row.source))
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
